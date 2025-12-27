import os
import re
import requests
import json
import logging
import random
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

# --- 1. 設定 API 金鑰庫 (安全讀取版) ---
# 從環境變數讀取金鑰，而不是寫死在程式碼裡
raw_keys = [
    os.environ.get('GEMINI_API_KEY_1'),
    os.environ.get('GEMINI_API_KEY_2'),
    os.environ.get('GEMINI_API_KEY_3'),
    os.environ.get('GEMINI_API_KEY') # 兼容舊設定
]

# 過濾掉空的 Key (避免讀到 None)
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

# --- 5. 核心功能：AI 寫文章 (多金鑰安全版) ---
def summarize_text(text):
    prompt = f"""
    你是一位專業主編。請閱讀以下影片內容，用「繁體中文」撰寫一篇重點懶人包。
    【內容】
    {text[:30000]}
    """

    # 你的可用模型清單 (根據之前 Log 確認過的)
    priority_models = [
        "gemini-2.5-flash",        
        "gemini-2.0-flash-exp",    
        "gemini-2.5-flash-lite",   
        "gemini-2.0-flash-lite-preview-02-05" 
    ]

    # --- 智慧金鑰輪替邏輯 ---
    # 複製金鑰池 (避免影響全域變數)
    keys_to_try = API_KEY_POOL.copy()
    random.shuffle(keys_to_try) # 隨機洗牌，達成負載平衡
    
    last_error = ""

    for key_index, current_key in enumerate(keys_to_try):
        # 隱碼處理 log
        masked_key = current_key[:5] + "..." + current_key[-4:]
        logger.info(f"🔑 [Key {key_index+1}/{len(keys_to_try)}] 切換金鑰: {masked_key}")
        
        genai.configure(api_key=current_key)

        for model_name in priority_models:
            try:
                logger.info(f"🤖 嘗試模型: {model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt, safety_settings=safety_settings)
                return response.text 
            except Exception as e:
                error_msg = str(e)
                if "429" in error_msg:
                    logger.warning(f"⚠️ 額度不足 (429) - 模型: {model_name}，準備切換...")
                elif "404" in error_msg:
                    logger.warning(f"⚠️ 模型未授權 (404) - 模型: {model_name}，跳過...")
                else:
                    logger.error(f"❌ 錯誤: {error_msg}")
                
                last_error = error_msg
                continue 

    return f"AI 生成失敗 (已嘗試 {len(keys_to_try)} 組 Key)。最後錯誤: {last_error}"

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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！多核心 AI 分析中..."))
        except: pass
        source, content = get_video_content(msg)
        if source == "失敗" or source == "錯誤":
            result_msg = f"❌ {content}"
        else:
            summary = summarize_text(content)
            result_msg = f"✅ 分析完成 ({source})\n\n{summary}"
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=result_msg))
        except: pass

if __name__ == "__main__":
    app.run()
