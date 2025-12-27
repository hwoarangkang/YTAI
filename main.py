import os
import re
import requests
import json
import logging
import random
import threading
import time
import tempfile
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from groq import Groq
import yt_dlp

# --- 設定 Log ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- 1. 設定 API 金鑰庫 ---
raw_keys = [
    os.environ.get('GEMINI_API_KEY_1'),
    os.environ.get('GEMINI_API_KEY_2'),
    os.environ.get('GEMINI_API_KEY_3'),
    os.environ.get('GEMINI_API_KEY') 
]
API_KEY_POOL = [k for k in raw_keys if k and k.strip()]

if not API_KEY_POOL:
    logger.error("❌ 嚴重錯誤: 環境變數中找不到任何 GEMINI_API_KEY！")

# LINE 設定
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

# --- 2. 設定 Gemini 安全過濾 ---
safety_settings = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# --- 3. 替身網路: Invidious (備援方案) ---
INVIDIOUS_INSTANCES = [
    "https://inv.tux.pizza",
    "https://vid.puffyan.us",
    "https://invidious.jing.rocks",
    "https://inv.zzls.xyz",
    "https://invidious.nerdvpn.de",
    "https://invidious.privacydev.net",
    "https://invidious.drgns.space"
]

def download_via_invidious(video_id):
    instances = INVIDIOUS_INSTANCES.copy()
    random.shuffle(instances)
    
    for instance in instances:
        try:
            api_url = f"{instance}/api/v1/videos/{video_id}"
            resp = requests.get(api_url, timeout=6)
            if resp.status_code != 200: continue
            
            data = resp.json()
            if 'adaptiveFormats' not in data: continue
            
            audio_url = None
            for fmt in data['adaptiveFormats']:
                if 'audio' in fmt.get('type', ''):
                    audio_url = fmt.get('url')
                    break
            
            if not audio_url: continue
            
            logger.info(f"🎵 Invidious ({instance}) 下載中...")
            file_resp = requests.get(audio_url, stream=True, timeout=20)
            if file_resp.status_code != 200: continue
            
            filename = f"/tmp/{video_id}_inv.mp3"
            with open(filename, 'wb') as f:
                for chunk in file_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            if os.path.getsize(filename) < 10240:
                os.remove(filename)
                continue
                
            return filename
        except: continue
    return None

# --- Cookie 處理器 (讀取 Render 環境變數) ---
def create_cookie_file():
    # 這裡會讀取你在 Render 設定的 YOUTUBE_COOKIES
    cookie_content = os.environ.get('YOUTUBE_COOKIES')
    if not cookie_content:
        logger.warning("⚠️ 警告：找不到 YOUTUBE_COOKIES 環境變數！")
        return None
    
    try:
        # 建立暫存檔 (因為 yt-dlp 需要檔案路徑)
        fd, path = tempfile.mkstemp(suffix='.txt', text=True)
        with os.fdopen(fd, 'w') as f:
            f.write(cookie_content)
        logger.info(f"🍪 Cookie 憑證已成功掛載至暫存區: {path}")
        return path
    except Exception as e:
        logger.error(f"Cookie 建立失敗: {e}")
        return None

# --- 4. 核心功能：分析影片 ---
def get_video_content(video_url):
    try:
        if "v=" in video_url:
            video_id = video_url.split("v=")[-1].split("&")[0]
        elif "youtu.be" in video_url:
            video_id = video_url.split("/")[-1].split("?")[0]
        else:
            return "錯誤", "無法辨識網址"

        full_text = None
        source_type = "未知"

        # [策略 A] 官方字幕
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0]
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except: pass

        # [策略 B] yt-dlp (Cookie 驗證模式 - 最強主力)
        if not full_text:
            logger.info("啟動策略 B: yt-dlp (Cookie 驗證模式)...")
            cookie_path = create_cookie_file()
            
            ydl_opts = {
                'format': 'bestaudio/best', 
                'outtmpl': '/tmp/%(id)s.%(ext)s',
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'nocheckcertificate': True
            }
            
            if cookie_path:
                ydl_opts['cookiefile'] = cookie_path
            else:
                # 沒 Cookie 才用 Android 偽裝
                ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android']}}

            try:
                filename = None
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    if info: filename = ydl.prepare_filename(info)
                
                if filename and os.path.exists(filename):
                    if os.path.getsize(filename) > 10240: # 確保檔案大於 10KB
                        with open(filename, "rb") as file:
                            transcription = groq_client.audio.transcriptions.create(
                                file=(filename, file.read()), model="whisper-large-v3", response_format="text"
                            )
                        full_text = transcription
                        source_type = "語音轉錄(yt-dlp)"
                    if os.path.exists(filename): os.remove(filename)
            except Exception as e:
                logger.error(f"yt-dlp 失敗: {e}")
            finally:
                # 重要：用完把暫存檔刪掉，保持乾淨
                if cookie_path and os.path.exists(cookie_path):
                    os.remove(cookie_path)

        # [策略 C] Invidious 替身 (備援)
        if not full_text:
            logger.info("啟動策略 C: Invidious 替身下載...")
            audio_file = download_via_invidious(video_id)
            if audio_file:
                try:
                    with open(audio_file, "rb") as file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(audio_file, file.read()), 
                            model="whisper-large-v3", 
                            response_format="text"
                        )
                    full_text = transcription
                    source_type = "語音轉錄(Invidious)"
                    if os.path.exists(audio_file): os.remove(audio_file)
                except Exception as e:
                    logger.error(f"Groq 轉錄失敗: {e}")

        if not full_text:
            return "失敗", "所有方法皆失效。YouTube 封鎖了伺服器連線。"

        return source_type, full_text
    except Exception as e:
        return "錯誤", str(e)

# --- 5. AI 寫文章 ---
def summarize_text(text):
    prompt = f"""
    你是一位專業主編。請閱讀以下影片內容，用「繁體中文」撰寫一篇重點懶人包。
    【內容】
    {text[:30000]}
    """

    priority_models = [
        "gemini-2.5-flash",        
        "gemini-2.0-flash-exp",    
        "gemini-2.5-flash-lite",   
        "gemini-2.0-flash-lite-preview-02-05" 
    ]

    keys_to_try = API_KEY_POOL.copy()
    random.shuffle(keys_to_try) 
    
    last_error = ""

    for current_key in keys_to_try:
        genai.configure(api_key=current_key)
        for model_name in priority_models:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt, safety_settings=safety_settings)
                return response.text 
            except Exception as e:
                error_msg = str(e)
                if "429" in error_msg:
                    logger.warning(f"⚠️ 額度不足 (429) - {model_name}")
                elif "404" in error_msg:
                    logger.warning(f"⚠️ 模型未授權 (404) - {model_name}")
                else:
                    logger.error(f"❌ 錯誤: {error_msg}")
                last_error = error_msg
                continue 

    return f"AI 生成失敗。原因: {last_error}"

# --- 背景任務 ---
def process_video_task(user_id, reply_token, msg):
    try:
        source, content = get_video_content(msg)
        
        if source == "失敗" or source == "錯誤":
            result_msg = f"❌ {content}"
        else:
            summary = summarize_text(content)
            result_msg = f"✅ 分析完成 ({source})\n\n{summary}"
        
        line_bot_api.push_message(user_id, TextSendMessage(text=result_msg))
        logger.info(f"✅ 成功推播結果給用戶 {user_id}")

    except Exception as e:
        logger.error(f"背景任務失敗: {e}")
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 系統錯誤: {str(e)}"))
        except: pass

# --- 6. LINE Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id
    
    if "youtube.com" in msg or "youtu.be" in msg:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！驗證身份中，請稍候..."))
        except: pass

        thread = threading.Thread(target=process_video_task, args=(user_id, event.reply_token, msg))
        thread.start()

if __name__ == "__main__":
    app.run()
