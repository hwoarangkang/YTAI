import os
import re
import requests
import json
import logging
import random
import threading  # <--- 新增：多執行緒模組，用來跑背景任務
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

# --- 3. Piped 替身伺服器軍團 ---
PIPED_INSTANCES = [
    "https://pipedapi.tokhmi.xyz", 
    "https://api.piped.privacy.com.de",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.moomoo.me",
    "https://pipedapi.systemless.io",
    "https://pipedapi.smnz.de",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.drgns.space",
    "https://pipedapi.ducks.party",
    "https://pipedapi.lunar.icu",
    "https://pipedapi.r4fo.com",
    "https://pipedapi.frontendfriendly.xyz",
    "https://api.piped.mha.fi",
    "https://api.piped.chalios.xyz",
    "https://api.piped.leptons.xyz",
    "https://pipedapi.kavin.rocks", # 這家最近很慢，移到最後面
    "https://api.piped.yt"
]

def get_transcript_via_piped(video_id):
    instances = PIPED_INSTANCES.copy()
    random.shuffle(instances)

    for instance in instances:
        try:
            url = f"{instance}/streams/{video_id}"
            # 設定短超時 (3秒)，遇到慢的直接跳過，不要等
            response = requests.get(url, timeout=3) 
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
                sub_text = requests.get(target_sub['url'], timeout=5).text
                # 檢查是否為錯誤網頁
                if "<!DOCTYPE html>" in sub_text or "Bad Gateway" in sub_text or "Cloudflare" in sub_text:
                    continue

                clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', '', sub_text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text) 
                clean_text = re.sub(r'WEBVTT|Kind: captions|Language: .*', '', clean_text)
                lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
                
                final_text = " ".join(list(dict.fromkeys(lines)))
                if len(final_text) < 50: continue
                return final_text
        except Exception:
            continue
    return None

# Piped 音訊下載 (背景執行時不會卡死主程式)
def download_audio_via_piped(video_id):
    instances = PIPED_INSTANCES.copy()
    random.shuffle(instances)
    
    for instance in instances:
        try:
            url = f"{instance}/streams/{video_id}"
            resp = requests.get(url, timeout=4)
            if resp.status_code != 200: continue
            
            data = resp.json()
            audio_streams = data.get('audioStreams', [])
            if not audio_streams: continue
            
            target_audio = audio_streams[0]
            audio_url = target_audio['url']
            
            logger.info(f"🎵 正在從 {instance} 下載音訊...")
            
            # 使用 stream=True 避免記憶體爆掉
            audio_resp = requests.get(audio_url, stream=True, timeout=15)
            if audio_resp.status_code != 200: continue

            filename = f"/tmp/{video_id}.mp3"
            with open(filename, 'wb') as f:
                for chunk in audio_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return filename
        except Exception:
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

        # 策略 A: 官方 API
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0]
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except: pass

        # 策略 B: Piped 字幕
        if not full_text:
            proxy_text = get_transcript_via_piped(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"

        # 策略 C: Piped 音訊 + Groq (這是最花時間的步驟，必須在背景跑)
        if not full_text:
            logger.info("啟動策略 C: Piped 音訊轉錄...")
            audio_file = download_audio_via_piped(video_id)
            if audio_file:
                try:
                    with open(audio_file, "rb") as file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(audio_file, file.read()), 
                            model="whisper-large-v3", 
                            response_format="text"
                        )
                    full_text = transcription
                    source_type = "語音轉錄(Piped)"
                    if os.path.exists(audio_file): os.remove(audio_file)
                except Exception as e:
                    logger.error(f"Groq 轉錄失敗: {e}")

        # 策略 D: yt-dlp (最後手段)
        if not full_text:
            logger.info("啟動策略 D: yt-dlp 下載...")
            try:
                ydl_opts = {
                    'format': 'bestaudio/best', 
                    'outtmpl': '/tmp/%(id)s.%(ext)s', 
                    'noplaylist': True,
                    'quiet': True,
                    'no_warnings': True,
                    'ignoreerrors': True, 
                    'nocheckcertificate': True,
                    'user_agent': 'Mozilla/5.0'
                }
                filename = None
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    if info:
                        filename = ydl.prepare_filename(info)
                
                if filename and os.path.exists(filename):
                    with open(filename, "rb") as file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(filename, file.read()), model="whisper-large-v3", response_format="text"
                        )
                    if os.path.exists(filename): os.remove(filename)
                    full_text = transcription
                    source_type = "語音轉錄(yt-dlp)"
            except Exception as e:
                return "失敗", f"所有方法都失敗了: {str(e)}"

        if not full_text:
            return "失敗", "無法取得字幕或音訊 (影片可能受保護或太長)"

        return source_type, full_text
    except Exception as e:
        return "錯誤", str(e)

# --- 5. AI 寫文章 (多金鑰安全版) ---
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

# --- 背景任務處理器 (關鍵！防止超時) ---
def process_video_task(user_id, reply_token, msg):
    """這個函式會在背景執行，就算跑 5 分鐘也不會被 Render 殺掉"""
    try:
        # 執行耗時的下載與分析
        source, content = get_video_content(msg)
        
        if source == "失敗" or source == "錯誤":
            result_msg = f"❌ {content}"
        else:
            summary = summarize_text(content)
            result_msg = f"✅ 分析完成 ({source})\n\n{summary}"
        
        # 任務完成後，主動「推播」訊息給使用者 (Push Message)
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
        # 1. 先快速回覆 LINE 伺服器，證明我們活著 (避免已讀不回)
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！已啟動背景下載，這可能需要 1~2 分鐘，請稍候..."))
        except Exception as e:
            logger.error(f"回覆錯誤: {e}")

        # 2. 開啟「背景分身」去執行耗時任務
        # 這樣主程式就能馬上結束回應，不會被 Render 判定超時殺掉
        thread = threading.Thread(target=process_video_task, args=(user_id, event.reply_token, msg))
        thread.start()

if __name__ == "__main__":
    app.run()
