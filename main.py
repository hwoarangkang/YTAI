import os
import re
import requests
import json
import logging
import random
import threading
import time
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

# --- 3. Piped 替身伺服器 (作為最後備援) ---
PIPED_INSTANCES = [
    "https://pipedapi.tokhmi.xyz", 
    "https://api.piped.privacy.com.de",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.moomoo.me",
    "https://pipedapi.drgns.space",
    "https://pipedapi.ducks.party",
    "https://pipedapi.r4fo.com",
    "https://api.piped.chalios.xyz",
    "https://api.piped.leptons.xyz"
]

FAKE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://www.google.com/'
}

def get_transcript_via_piped(video_id):
    instances = PIPED_INSTANCES.copy()
    random.shuffle(instances)

    for instance in instances:
        try:
            url = f"{instance}/streams/{video_id}"
            response = requests.get(url, headers=FAKE_HEADERS, timeout=3) 
            if response.status_code != 200: continue
            
            data = response.json()
            subtitles = data.get('subtitles', [])
            if not subtitles: continue

            target_sub = None
            priority_langs = ['zh-TW', 'zh-Hant', 'zh', 'zh-CN', 'en']
            for lang in priority_langs:
                for sub in subtitles:
                    if lang in sub.get('code', ''):
                        target_sub = sub
                        break
                if target_sub: break
            
            if not target_sub and subtitles: target_sub = subtitles[0]

            if target_sub:
                sub_text = requests.get(target_sub['url'], headers=FAKE_HEADERS, timeout=5).text
                if "<!DOCTYPE html>" in sub_text or "Bad Gateway" in sub_text: continue

                clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', '', sub_text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text) 
                clean_text = re.sub(r'WEBVTT|Kind: captions|Language: .*', '', clean_text)
                lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
                return " ".join(list(dict.fromkeys(lines)))
        except: continue
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

        # [策略 A] 官方 API (最快，優先嘗試)
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0]
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except: pass

        # [策略 B] yt-dlp Android 偽裝模式 (強力突圍)
        # 這是 V21.0 的核心：不使用網頁版 API，而是模擬 Android 客戶端
        if not full_text:
            logger.info("啟動策略 B: yt-dlp (Android 偽裝模式)...")
            try:
                ydl_opts = {
                    'format': 'bestaudio/best', 
                    'outtmpl': '/tmp/%(id)s.%(ext)s',
                    'noplaylist': True,
                    'quiet': True,
                    'no_warnings': True,
                    'ignoreerrors': True,
                    'nocheckcertificate': True,
                    # 🔥 關鍵參數：欺騙 YouTube 我們是 Android 手機 App 🔥
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['android', 'ios'],
                            'skip': ['dash', 'hls']
                        }
                    }
                }
                
                filename = None
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    if info:
                        filename = ydl.prepare_filename(info)
                
                if filename and os.path.exists(filename):
                    # 檔案太小可能是下載失敗的垃圾檔
                    if os.path.getsize(filename) < 10240:
                        os.remove(filename)
                    else:
                        with open(filename, "rb") as file:
                            transcription = groq_client.audio.transcriptions.create(
                                file=(filename, file.read()), model="whisper-large-v3", response_format="text"
                            )
                        if os.path.exists(filename): os.remove(filename)
                        full_text = transcription
                        source_type = "語音轉錄(Android偽裝)"
            except Exception as e:
                logger.error(f"yt-dlp Android 模式失敗: {e}")

        # [策略 C] Piped 替身 (最後備援)
        if not full_text:
            proxy_text = get_transcript_via_piped(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"
        
        if not full_text:
            return "失敗", "所有策略皆失效 (YouTube 阻擋了伺服器連線)"

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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！啟動 Android 模擬分析模式..."))
        except: pass

        thread = threading.Thread(target=process_video_task, args=(user_id, event.reply_token, msg))
        thread.start()

if __name__ == "__main__":
    app.run()
