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

# --- 3. Cobalt API 節點 (借刀殺人策略) ---
# 這些是公開的 Cobalt 實例，用來幫我們繞過 YouTube 的 IP 封鎖
COBALT_INSTANCES = [
    "https://api.cobalt.tools", # 官方主節點
    "https://cobalt.kwiatekmiki.com",
    "https://cobalt.q1.pm",
    "https://cobalt.kinuseka.net"
]

def download_audio_via_cobalt(video_url):
    """使用 Cobalt API 下載音訊，繞過 Render IP 封鎖"""
    instances = COBALT_INSTANCES.copy()
    random.shuffle(instances)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    payload = {
        "url": video_url,
        "videoQuality": "144", # 我們只要音訊，畫質最低即可
        "audioFormat": "mp3",
        "isAudioOnly": True    # 強制只抓音訊
    }

    for instance in instances:
        try:
            logger.info(f"🚀 呼叫 Cobalt 節點: {instance}")
            api_url = f"{instance}/api/json" # Cobalt API 端點
            
            # 1. 請求下載連結
            resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"Cobalt {instance} 回應錯誤: {resp.status_code}")
                continue
            
            data = resp.json()
            
            # 檢查 API 回傳狀態
            if data.get("status") == "error":
                logger.warning(f"Cobalt 錯誤: {data.get('text')}")
                continue

            download_url = data.get("url")
            if not download_url: continue

            logger.info(f"🎵 取得 Cobalt 下載連結，開始下載...")

            # 2. 下載實體檔案
            file_resp = requests.get(download_url, stream=True, timeout=20)
            if file_resp.status_code != 200: continue

            video_id = video_url.split("v=")[-1].split("&")[0] if "v=" in video_url else "temp"
            filename = f"/tmp/{video_id}_cobalt.mp3"

            with open(filename, 'wb') as f:
                for chunk in file_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # 檢查檔案大小，太小可能是錯誤檔
            if os.path.getsize(filename) < 10240:
                os.remove(filename)
                continue

            return filename

        except Exception as e:
            logger.error(f"Cobalt 下載失敗: {e}")
            continue
    
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

        # [策略 B] Cobalt API + Groq (最強兵器)
        # 既然 Render IP 被鎖，我們就叫 Cobalt 幫我們去抓
        if not full_text:
            logger.info("啟動策略 B: Cobalt API 音訊下載...")
            audio_file = download_audio_via_cobalt(video_url)
            
            if audio_file:
                try:
                    with open(audio_file, "rb") as file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(audio_file, file.read()), 
                            model="whisper-large-v3", 
                            response_format="text"
                        )
                    full_text = transcription
                    source_type = "語音轉錄(Cobalt)"
                    if os.path.exists(audio_file): os.remove(audio_file)
                except Exception as e:
                    logger.error(f"Groq 轉錄失敗: {e}")

        if not full_text:
            return "失敗", "官方字幕無效，且外部下載服務皆忙線中 (IP 被針對)。"

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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！啟動 Cobalt 協議下載影片..."))
        except: pass

        thread = threading.Thread(target=process_video_task, args=(user_id, event.reply_token, msg))
        thread.start()

if __name__ == "__main__":
    app.run()
