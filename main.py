import os
import re
import requests
import json
import logging
import random
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

# --- 3. Piped 替身伺服器軍團 (人海戰術) ---
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.privacy.com.de",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.moomoo.me",
    "https://api.piped.yt",
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
    "https://api.piped.leptons.xyz"
]

def get_transcript_via_piped(video_id):
    instances = PIPED_INSTANCES.copy()
    random.shuffle(instances)

    for instance in instances:
        try:
            url = f"{instance}/streams/{video_id}"
            response = requests.get(url, timeout=4) 
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

# --- 新增功能：從 Piped 抓取音訊檔 (繞過 yt-dlp 問題) ---
def download_audio_via_piped(video_id):
    instances = PIPED_INSTANCES.copy()
    random.shuffle(instances)
    
    for instance in instances:
        try:
            # 1. 取得串流資訊
            url = f"{instance}/streams/{video_id}"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200: continue
            
            data = resp.json()
            audio_streams = data.get('audioStreams', [])
            if not audio_streams: continue
            
            # 2. 挑選第一個可用的音訊流 (通常是 m4a 或 webm)
            target_audio = audio_streams[0]
            audio_url = target_audio['url']
            
            logger.info(f"🎵 嘗試從 {instance} 下載音訊...")
            
            # 3. 下載檔案
            audio_resp = requests.get(audio_url, stream=True, timeout=15)
            if audio_resp.status_code != 200: continue

            filename = f"/tmp/{video_id}.mp3" # Groq 支援多種格式，副檔名用 mp3 通吃
            with open(filename, 'wb') as f:
                for chunk in audio_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return filename
        except Exception as e:
            # logger.warning(f"Piped Audio Fail: {e}")
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

        # [策略 A] 官方 API
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0]
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except: pass

        # [策略 B] Piped 字幕
        if not full_text:
            proxy_text = get_transcript_via_piped(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"

        # [策略 C] Piped 音訊下載 + Groq (新招！繞過 Render 無法執行 JS 的問題)
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

        # [策略 D] yt-dlp 下載 (最後手段，已放寬格式限制)
        if not full_text:
            logger.info("啟動策略 D: yt-dlp 下載...")
            try:
                ydl_opts = {
                    'format': 'bestaudio/best', # 不挑食，有什麼吃什麼
                    'outtmpl': '/tmp/%(id)s.%(ext)s', 
                    'noplaylist': True,
                    'quiet': True,
                    'no_warnings': True,
                    'ignoreerrors': True, # 遇到小錯誤不要崩潰
                    'nocheckcertificate': True,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
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
                return "失敗", f"無法取得內容 (所有策略皆失敗): {str(e)}"

        if not full_text:
            return "失敗", "無法取得字幕或音訊 (影片可能受保護或無效)"

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

    priority_models = [
        "gemini-2.5-flash",        
        "gemini-2.0-flash-exp",    
        "gemini-2.5-flash-lite",   
        "gemini-2.0-flash-lite-preview-02-05" 
    ]

    keys_to_try = API_KEY_POOL.copy()
    random.shuffle(keys_to_try) 
    
    last_error = ""

    for key_index, current_key in enumerate(keys_to_try):
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
                    logger.warning(f"⚠️ 額度不足 (429) - {model_name}")
                elif "404" in error_msg:
                    logger.warning(f"⚠️ 模型未授權 (404) - {model_name}")
                else:
                    logger.error(f"❌ 錯誤: {error_msg}")
                last_error = error_msg
                continue 

    return f"AI 生成失敗。原因: {last_error}"

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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！正在調度全球節點下載影片..."))
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
