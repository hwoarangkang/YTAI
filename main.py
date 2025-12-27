import os
import re
import requests
import json
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from groq import Groq
import yt_dlp

app = Flask(__name__)

# --- 1. 設定 API 金鑰 ---
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))
groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

# --- 2. 設定 Gemini 安全過濾 (防止拒絕生成) ---
safety_settings = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# --- 3. 定義 Piped 替身伺服器 (繞過 YouTube 封鎖用) ---
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.privacy.com.de",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.moomoo.me",
    "https://api.piped.yt"
]

# --- 輔助功能：透過 Piped 抓字幕 ---
def get_transcript_via_piped(video_id):
    for instance in PIPED_INSTANCES:
        try:
            print(f"正在嘗試替身伺服器: {instance} ...")
            url = f"{instance}/streams/{video_id}"
            response = requests.get(url, timeout=5)
            
            if response.status_code != 200: continue
                
            data = response.json()
            subtitles = data.get('subtitles', [])
            
            if not subtitles: continue

            target_sub = None
            # 優先順序：繁中 -> 簡中 -> 英文
            priority_langs = ['zh-TW', 'zh-Hant', 'zh', 'zh-CN', 'en']
            
            for lang in priority_langs:
                for sub in subtitles:
                    if lang in sub.get('code', ''):
                        target_sub = sub
                        break
                if target_sub: break
            
            # 若無指定語言，抓第一個 (包含 auto-generated)
            if not target_sub and subtitles:
                target_sub = subtitles[0]

            if target_sub:
                print(f"成功從 {instance} 抓到字幕")
                sub_text = requests.get(target_sub['url']).text
                
                # 清理 VTT 格式雜訊
                clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', '', sub_text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text) 
                clean_text = re.sub(r'WEBVTT|Kind: captions|Language: .*', '', clean_text)
                
                # 去除重複行並合併
                lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
                return " ".join(list(dict.fromkeys(lines)))

        except Exception as e:
            print(f"{instance} 連線失敗: {e}")
            continue
            
    return None

# --- 4. 核心功能：分析影片 (多重策略) ---
def get_video_content(video_url):
    try:
        # 解析 Video ID
        if "v=" in video_url:
            video_id = video_url.split("v=")[-1].split("&")[0]
        elif "youtu.be" in video_url:
            video_id = video_url.split("/")[-1].split("?")[0]
        else:
            return "錯誤", "無法辨識網址"

        full_text = None
        source_type = "未知"

        # [策略 A] 正規軍 (YouTubeTranscriptApi)
        try:
            print("策略 A: 正規抓取")
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0] # 抓取任意可用字幕
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except Exception:
            print("正規抓取失敗，切換策略 B...")

        # [策略 B] 替身軍團 (Piped API) - 最強防封鎖
        if not full_text:
            print("策略 B: 啟動多重替身輪詢")
            proxy_text = get_transcript_via_piped(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"

        # [策略 C] 下載音訊轉錄 (Groq) - 最後手段
        if not full_text:
            try:
                print("策略 C: 嘗試語音轉錄 (Groq)...")
                ydl_opts = {
                    'format': 'bestaudio[ext=m4a]/bestaudio', 
                    'outtmpl': '/tmp/%(id)s.%(ext)s',
                    'noplaylist': True,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    filename = ydl.prepare_filename(info)
                
                with open(filename, "rb") as file:
                    transcription = groq_client.audio.transcriptions.create(
                        file=(filename, file.read()),
                        model="whisper-large-v3",
                        response_format="text"
                    )
                if os.path.exists(filename): os.remove(filename)
                full_text = transcription
                source_type = "語音轉錄(Groq)"
            except Exception as e:
                return "失敗", f"所有方法都失效 (無字幕且下載被阻擋): {str(e)}"

        return source_type, full_text

    except Exception as e:
        return "錯誤", str(e)

# --- 5. 核心功能：AI 寫文章 (智慧雙模組 V17.22) ---
def summarize_text(text):
    prompt = f"""
    你是一位專業主編。請閱讀以下影片內容，用「繁體中文」撰寫一篇重點懶人包。
    
    【要求】
    1. 標題：吸睛且精準。
    2. 結構：前言、核心重點（條列式）、結論。
    3. 語氣：通順流暢，去除逐字稿的口語贅字。
    
    【內容】
    {text[:30000]}
    """

    # 定義模型優先順序 (模擬你的 JS 排序邏輯)
    # 1. gemini-2.0-flash-exp: 目前最新的實驗版 (對應坊間的 2.5)
    # 2. gemini-1.5-flash: 穩定且快速
    priority_models = [
        "gemini-2.0-flash-exp", 
        "gemini-1.5-flash"
    ]

    last_error = ""

    for model_name in priority_models:
        try:
            print(f"嘗試使用模型: {model_name} ...")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt, safety_settings=safety_settings)
            return response.text # 成功則直接回傳
        except Exception as e:
            print(f"模型 {model_name} 失敗: {e}")
            last_error = str(e)
            continue # 失敗則嘗試下一個模型

    return f"AI 生成失敗 (所有模型皆嘗試過): {last_error}"

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
        # 回覆處理中
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！正在啟動多重路徑分析影片 (約需 20~60 秒)..."))
        except: pass

        # 執行分析
        source, content = get_video_content(msg)
        
        if source == "失敗" or source == "錯誤":
            result_msg = f"❌ {content}"
        else:
            summary = summarize_text(content)
            result_msg = f"✅ 分析完成 (來源: {source})\n\n{summary}"
        
        # 推播結果
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=result_msg))
        except Exception as e:
            print(f"Push error: {e}")

if __name__ == "__main__":
    app.run()
