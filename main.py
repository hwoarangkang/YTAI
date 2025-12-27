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

# --- 3. 蟲群節點庫 (Invidious + Piped + Cobalt) ---
# 包含全球各地的節點，只要一個能通就贏了
PROXY_SWARM = [
    # Invidious Instances
    "https://inv.tux.pizza", "https://vid.puffyan.us", "https://invidious.jing.rocks",
    "https://inv.zzls.xyz", "https://invidious.nerdvpn.de", "https://invidious.privacydev.net",
    "https://invidious.drgns.space", "https://yt.artemislena.eu", "https://invidious.flokinet.to",
    "https://invidious.projectsegfau.lt", "https://yewtu.be", "https://invidious.einfachzocken.eu",
    # Piped Instances
    "https://pipedapi.tokhmi.xyz", "https://api.piped.privacy.com.de", "https://api.piped.projectsegfau.lt",
    "https://pipedapi.moomoo.me", "https://pipedapi.drgns.space", "https://pipedapi.ducks.party",
    "https://pipedapi.r4fo.com", "https://api.piped.chalios.xyz", "https://api.piped.leptons.xyz",
    "https://pipedapi.kavin.rocks", "https://api.piped.yt",
    # Cobalt (偶爾會活)
    "https://api.cobalt.tools", "https://cobalt.kwiatekmiki.com", "https://cobalt.q1.pm"
]

FAKE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.google.com/"
}

def download_from_swarm(video_id, video_url):
    """執行蟲群下載戰術"""
    nodes = PROXY_SWARM.copy()
    random.shuffle(nodes) # 隨機打亂，避免塞車
    
    logger.info(f"🐝 啟動蟲群戰術，可用節點數: {len(nodes)}")

    for node in nodes:
        try:
            # 判斷是哪種 API (Invidious/Piped/Cobalt)
            if "piped" in node or "api.piped" in node:
                # Piped 邏輯
                api_url = f"{node}/streams/{video_id}"
                resp = requests.get(api_url, headers=FAKE_HEADERS, timeout=4)
                if resp.status_code != 200: continue
                data = resp.json()
                audio_streams = data.get('audioStreams', [])
                if not audio_streams: continue
                dl_url = next((s['url'] for s in audio_streams if s.get('format') == 'm4a'), audio_streams[0]['url'])
                
            elif "cobalt" in node:
                # Cobalt 邏輯
                headers = {"Accept": "application/json", "Content-Type": "application/json"}
                payload = {"url": video_url, "audioFormat": "mp3", "isAudioOnly": True}
                resp = requests.post(f"{node}/api/json", json=payload, headers=headers, timeout=4)
                if resp.status_code != 200: continue
                data = resp.json()
                dl_url = data.get("url")
                if not dl_url: continue

            else:
                # Invidious 邏輯 (預設)
                api_url = f"{node}/api/v1/videos/{video_id}"
                resp = requests.get(api_url, timeout=4)
                if resp.status_code != 200: continue
                data = resp.json()
                if 'adaptiveFormats' not in data: continue
                dl_url = None
                for fmt in data['adaptiveFormats']:
                    if 'audio' in fmt.get('type', ''):
                        dl_url = fmt.get('url')
                        break
                if not dl_url: continue

            # --- 執行下載 ---
            logger.info(f"🐝 節點命中: {node}，下載中...")
            file_resp = requests.get(dl_url, headers=FAKE_HEADERS, stream=True, timeout=15)
            if file_resp.status_code != 200: continue
            
            filename = f"/tmp/{video_id}.mp3"
            with open(filename, 'wb') as f:
                for chunk in file_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            # 驗證檔案
            if os.path.getsize(filename) > 5000: # 大於 5KB 才算成功
                return filename, node
            else:
                os.remove(filename)
                
        except Exception:
            continue # 失敗就秒換下一個，不要停

    return None, None

# --- Cookie 處理器 ---
def get_cookie_path():
    if os.path.exists('cookies.txt'):
        return 'cookies.txt'
    return None

# --- Gemini 檔案上傳處理 (大檔案專用) ---
def summarize_large_audio_with_gemini(audio_path):
    try:
        logger.info("🐘 切換至 Gemini 原生聽力模式...")
        current_key = random.choice(API_KEY_POOL)
        genai.configure(api_key=current_key)
        
        myfile = genai.upload_file(audio_path)
        while myfile.state.name == "PROCESSING":
            time.sleep(2)
            myfile = genai.get_file(myfile.name)

        if myfile.state.name == "FAILED": raise ValueError("Gemini File Failed")

        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = "你是一位專業主編。請聽這段音訊內容，用「繁體中文」撰寫一篇詳細的重點懶人包。內容要結構清晰，包含標題和條列式重點。"
        result = model.generate_content([myfile, prompt], safety_settings=safety_settings)
        genai.delete_file(myfile.name)
        return result.text
    except Exception as e:
        return f"Gemini 分析失敗: {str(e)}"

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

        # [策略 B] 蟲群戰術 (優先於 yt-dlp，因為不會被 IP 鎖)
        logger.info("🚀 啟動策略 B: 全球蟲群節點下載...")
        audio_file, node_name = download_from_swarm(video_id, video_url)
        if audio_file:
            source_type = f"替身節點({node_name})"

        # [策略 C] yt-dlp (最後防線，使用 Cookie)
        if not audio_file:
            logger.info("⚠️ 蟲群失敗，啟動策略 C: yt-dlp (Cookie 核彈)...")
            cookie_path = get_cookie_path()
            ydl_opts = {
                'format': 'worstaudio/worst',
                'outtmpl': '/tmp/%(id)s.%(ext)s',
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'nocheckcertificate': True
            }
            if cookie_path: ydl_opts['cookiefile'] = cookie_path
            else: ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android']}}

            try:
                filename = None
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    if info: filename = ydl.prepare_filename(info)
                if filename and os.path.exists(filename) and os.path.getsize(filename) > 5000:
                    audio_file = filename
                    source_type = "yt-dlp"
            except Exception as e:
                logger.error(f"yt-dlp 失敗: {e}")

        # --- 分流處理 ---
        if audio_file:
            file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
            logger.info(f"🎵 檔案大小: {file_size_mb:.2f} MB")

            try:
                if file_size_mb < 24:
                    logger.info("⚡ 小檔案 -> Groq")
                    with open(audio_file, "rb") as file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(audio_file, file.read()), model="whisper-large-v3", response_format="text"
                        )
                    content = transcription
                    source_desc = f"語音轉錄({source_type}/Groq)"
                else:
                    logger.info("🐘 大檔案 -> Gemini")
                    summary = summarize_large_audio_with_gemini(audio_file)
                    if os.path.exists(audio_file): os.remove(audio_file)
                    return f"Gemini聽力({source_type})", summary

                if os.path.exists(audio_file): os.remove(audio_file)
                return source_desc, content

            except Exception as e:
                if os.path.exists(audio_file): os.remove(audio_file)
                return "失敗", f"轉錄錯誤: {str(e)}"

        return "失敗", "所有下載策略皆失敗 (官方字幕/蟲群節點/Cookie驗證)。"

    except Exception as e:
        return "錯誤", str(e)

# --- 5. AI 寫文章 ---
def summarize_text(text):
    if text.startswith("##") or "懶人包" in text: return text
    prompt = f"你是一位專業主編。請閱讀以下內容，用「繁體中文」撰寫一篇重點懶人包。\n【內容】\n{text[:30000]}"
    
    priority_models = ["gemini-2.5-flash", "gemini-2.0-flash-exp", "gemini-2.5-flash-lite"]
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
            if "Gemini聽力" in source: summary = content
            else: summary = summarize_text(content)
            result_msg = f"✅ 分析完成 ({source})\n\n{summary}"
        
        line_bot_api.push_message(user_id, TextSendMessage(text=result_msg))
    except: pass

# --- 6. LINE Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    if "youtube.com" in msg or "youtu.be" in msg:
        try: line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！啟動「全球蟲群戰術」下載中..."))
        except: pass
        thread = threading.Thread(target=process_video_task, args=(event.source.user_id, event.reply_token, msg))
        thread.start()

if __name__ == "__main__":
    app.run()
