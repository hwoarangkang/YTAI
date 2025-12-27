import os
import re
import requests
import json
import logging
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

# --- 1. 設定 API 金鑰 ---
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

api_key = os.environ.get('GEMINI_API_KEY')
if not api_key:
    logger.error("❌ 嚴重錯誤: 找不到 GEMINI_API_KEY 環境變數！")
genai.configure(api_key=api_key)

groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

# --- 🚀 啟動時診斷：列出可用模型 (關鍵除錯步驟) ---
# 這段程式碼會告訴我們，你的 API Key 到底有沒有權限，以及能看到哪些模型。
try:
    logger.info("🔍 [診斷模式] 正在測試 API Key 連線與可用模型清單...")
    available_models = []
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            available_models.append(m.name)
    
    if available_models:
        logger.info(f"✅ API Key 測試成功！可用模型如下:\n{available_models}")
    else:
        logger.warning("⚠️ API Key 連線成功，但清單是空的 (可能沒有權限或地區限制)")
except Exception as e:
    logger.error(f"❌ API Key 測試失敗 (這就是 404 的原因): {e}")

# --- 2. 設定 Gemini 安全過濾 ---
safety_settings = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# --- 3. Piped 替身伺服器 ---
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.privacy.com.de",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.moomoo.me",
    "https://api.piped.yt"
]

def get_transcript_via_piped(video_id):
    for instance in PIPED_INSTANCES:
        try:
            url = f"{instance}/streams/{video_id}"
            response = requests.get(url, timeout=5)
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
                sub_text = requests.get(target_sub['url']).text
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

        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0]
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except: pass

        if not full_text:
            proxy_text = get_transcript_via_piped(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"

        if not full_text:
            try:
                ydl_opts = {'format': 'bestaudio[ext=m4a]/bestaudio', 'outtmpl': '/tmp/%(id)s.%(ext)s', 'noplaylist': True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    filename = ydl.prepare_filename(info)
                with open(filename, "rb") as file:
                    transcription = groq_client.audio.transcriptions.create(
                        file=(filename, file.read()), model="whisper-large-v3", response_format="text"
                    )
                if os.path.exists(filename): os.remove(filename)
                full_text = transcription
                source_type = "語音轉錄(Groq)"
            except Exception as e:
                return "失敗", f"無字幕且轉錄失敗: {str(e)}"

        return source_type, full_text
    except Exception as e:
        return "錯誤", str(e)

# --- 5. 核心功能：AI 寫文章 (動態清單版) ---
def summarize_text(text):
    prompt = f"""
    你是一位專業主編。請閱讀以下影片內容，用「繁體中文」撰寫一篇重點懶人包。
    【內容】
    {text[:30000]}
    """

    # 優先順序：2.0 Flash -> 1.5 Flash 002 -> 1.5 Flash -> 1.5 Pro
    priority_models = [
        "gemini-2.0-flash-exp", 
        "gemini-1.5-flash-002",
        "gemini-1.5-flash",
        "gemini-1.5-pro"
    ]

    last_error = ""
    
    # 這裡的改動：印出我們正在嘗試哪個模型
    for model_name in priority_models:
        try:
            logger.info(f"🤖 正在呼叫模型: {model_name}")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt, safety_settings=safety_settings)
            return response.text
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ 模型 {model_name} 失敗: {error_msg}")
            last_error = error_msg
            continue

    return f"AI 全部失敗 (Code 0.8.6)。錯誤原因: {last_error}"

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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 分析運算中..."))
        except: pass
        source, content = get_video_content(msg)
        if source == "失敗" or source == "錯誤":
            result_msg = f"❌ {content}"
        else:
            summary = summarize_text(content)
            result_msg = f"✅ 完成 ({source})\n\n{summary}"
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=result_msg))
        except: pass

if __name__ == "__main__":
    app.run()
