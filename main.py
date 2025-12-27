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

# --- 3. 替身網路: Invidious (備援) ---
INVIDIOUS_INSTANCES = [
    "https://inv.tux.pizza",
    "https://vid.puffyan.us",
    "https://invidious.jing.rocks",
    "https://inv.zzls.xyz",
    "https://invidious.nerdvpn.de"
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

# --- Cookie 處理器 (V30 改良版：優先讀檔) ---
def get_cookie_path():
    # 1. 最優先：檢查 GitHub 專案裡有沒有 cookies.txt
    if os.path.exists('cookies.txt'):
        logger.info("🍪 發現本地 cookies.txt 檔案，優先使用！(格式最穩)")
        return 'cookies.txt'
    
    # 2. 次要：檢查環境變數 (容易格式跑掉，當作備用)
    cookie_content = os.environ.get('YOUTUBE_COOKIES')
    if cookie_content:
        try:
            fd, path = tempfile.mkstemp(suffix='.txt', text=True)
            with os.fdopen(fd, 'w') as f:
                f.write(cookie_content)
            logger.info(f"🍪 使用環境變數建立臨時 Cookie: {path}")
            return path
        except: pass
        
    logger.warning("⚠️ 未偵測到任何 Cookie，將嘗試裸連 (失敗率高)...")
    return None

# --- Gemini 檔案上傳處理 (大檔案專用) ---
def summarize_large_audio_with_gemini(audio_path):
    """使用 Gemini 1.5 Flash 直接聽音檔 (繞過 Groq 25MB 限制)"""
    try:
        logger.info("🐘 檔案過大，切換至 Gemini 1.5 Flash 原生聽力模式...")
        
        current_key = random.choice(API_KEY_POOL)
        genai.configure(api_key=current_key)
        
        myfile = genai.upload_file(audio_path)
        logger.info(f"📤 檔案上傳中: {myfile.name}")

        while myfile.state.name == "PROCESSING":
            time.sleep(2)
            myfile = genai.get_file(myfile.name)

        if myfile.state.name == "FAILED":
            raise ValueError("Gemini 檔案處理失敗")

        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = "你是一位專業主編。請聽這段音訊內容，用「繁體中文」撰寫一篇詳細的重點懶人包。內容要結構清晰，包含標題和條列式重點。"
        
        result = model.generate_content([myfile, prompt], safety_settings=safety_settings)
        genai.delete_file(myfile.name)
        return result.text

    except Exception as e:
        return f"Gemini 音訊分析失敗: {str(e)}"

# --- 4. 核心功能：分析影片 ---
def get_video_content(video_url):
    try:
        if "v=" in video_url:
            video_id = video_url.split("v=")[-1].split("&")[0]
        elif "youtu.be" in video_url:
            video_id = video_url.split("/")[-1].split("?")[0]
        else:
            return "錯誤", "無法辨識網址"

        # [策略 A] 官方字幕
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0]
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            return "CC字幕(官方)", full_text
        except: pass

        audio_file = None
        source_type = "未知"

        # [策略 B] yt-dlp (Cookie 檔案驗證 + 瘦身)
        logger.info("啟動策略 B: yt-dlp (Cookie/瘦身模式)...")
        cookie_path = get_cookie_path() # 使用新的路徑取得函式
        
        ydl_opts = {
            'format': 'worstaudio/worst',
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
            ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android']}}

        try:
            filename = None
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                if info: filename = ydl.prepare_filename(info)
            
            if filename and os.path.exists(filename):
                if os.path.getsize(filename) > 10240:
                    audio_file = filename
                    source_type = "yt-dlp"
        except Exception as e:
            logger.error(f"yt-dlp 失敗: {e}")
        finally:
            # 只刪除臨時產生的 cookie，如果是上傳的 cookies.txt 則保留
            if cookie_path and cookie_path != 'cookies.txt' and os.path.exists(cookie_path):
                os.remove(cookie_path)

        # [策略 C] Invidious
        if not audio_file:
            logger.info("啟動策略 C: Invidious 替身...")
            audio_file = download_via_invidious(video_id)
            if audio_file: source_type = "Invidious"

        # --- 分流處理 ---
        if audio_file:
            file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
            logger.info(f"🎵 音訊檔案大小: {file_size_mb:.2f} MB")

            try:
                # 24MB 為界線
                if file_size_mb < 24:
                    logger.info("⚡ 小檔案，使用 Groq 轉錄...")
                    with open(audio_file, "rb") as file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(audio_file, file.read()), model="whisper-large-v3", response_format="text"
                        )
                    content = transcription
                    source_desc = f"語音轉錄({source_type}/Groq)"
                else:
                    logger.info("🐘 大檔案，使用 Gemini 原生分析...")
                    summary = summarize_large_audio_with_gemini(audio_file)
                    if os.path.exists(audio_file): os.remove(audio_file)
                    return f"Gemini聽力({source_type})", summary

                if os.path.exists(audio_file): os.remove(audio_file)
                return source_desc, content

            except Exception as e:
                if os.path.exists(audio_file): os.remove(audio_file)
                return "失敗", f"轉錄/分析過程發生錯誤: {str(e)}"

        return "失敗", "無法下載內容 (請確認 cookies.txt 是否已上傳至 GitHub)"

    except Exception as e:
        return "錯誤", str(e)

# --- 5. AI 寫文章 ---
def summarize_text(text):
    if text.startswith("##") or "懶人包" in text:
        return text

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
                last_error = str(e)
                continue 

    return f"AI 生成失敗。原因: {last_error}"

# --- 背景任務 ---
def process_video_task(user_id, reply_token, msg):
    try:
        source, content = get_video_content(msg)
        
        if source == "失敗" or source == "錯誤":
            result_msg = f"❌ {content}"
        else:
            if "Gemini聽力" in source:
                summary = content
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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！啟用「V30 實體Cookie」分析..."))
        except: pass

        thread = threading.Thread(target=process_video_task, args=(user_id, event.reply_token, msg))
        thread.start()

if __name__ == "__main__":
    app.run()
