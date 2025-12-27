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

app = Flask(__name__)

# 從環境變數讀取鑰匙
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))

# --- 設定 AI 安全過濾器 (關閉所有限制) ---
# 這是關鍵！把所有過濾器都設為 BLOCK_NONE，避免商業新聞被誤判
safety_settings = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}

# 定義多個 Piped 替身伺服器
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
            print(f"嘗試替身: {instance}")
            url = f"{instance}/streams/{video_id}"
            response = requests.get(url, timeout=5)
            if response.status_code != 200: continue
            
            data = response.json()
            subtitles = data.get('subtitles', [])
            if not subtitles: continue

            target_sub = None
            for code in ['zh-TW', 'zh-Hant', 'zh', 'zh-CN', 'en']: # 優先順序
                for sub in subtitles:
                    if code in sub.get('code', ''):
                        target_sub = sub
                        break
                if target_sub: break
            
            if not target_sub and subtitles: target_sub = subtitles[0]

            if target_sub:
                sub_text = requests.get(target_sub['url']).text
                # 清理字幕雜訊
                clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', '', sub_text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text) 
                clean_text = re.sub(r'WEBVTT|Kind: captions|Language: .*', '', clean_text)
                lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
                return " ".join(list(dict.fromkeys(lines))) # 去重並合併

        except Exception:
            continue
    return None

def get_video_content(video_url):
    try:
        if "v=" in video_url: video_id = video_url.split("v=")[-1].split("&")[0]
        elif "youtu.be" in video_url: video_id = video_url.split("/")[-1].split("?")[0]
        else: return "錯誤", "無法辨識網址"

        full_text = None
        source_type = "未知"

        # 策略 A: 正規軍
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = list(transcript_list)[0]
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except:
            pass

        # 策略 B: 替身軍團
        if not full_text:
            proxy_text = get_transcript_via_piped(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"
            else:
                return "失敗", "所有字幕伺服器皆忙碌或無字幕，請稍後再試。"

        return source_type, full_text
    except Exception as e:
        return "錯誤", str(e)

def summarize_text(text):
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"""
    你是一位專業主編。請閱讀以下影片字幕，用「繁體中文」寫成一篇重點懶人包。
    要求：標題吸睛、包含【前言】【核心重點】(條列式)【結論】。
    內容：
    {text[:30000]}
    """
    try:
        # 這裡加入了 safety_settings
        response = model.generate_content(prompt, safety_settings=safety_settings)
        return response.text
    except Exception as e:
        return f"AI 生成失敗: {str(e)}" # 讓錯誤訊息顯示出來

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text
    if "youtube.com" in msg or "youtu.be" in msg:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！正在分析中..."))
        source, content = get_video_content(msg)
        if source == "失敗" or source == "錯誤":
            reply = f"❌ {content}"
        else:
            summary = summarize_text(content)
            reply = f"✅ 分析完成 (來源: {source})\n\n{summary}"
        try: line_bot_api.push_message(event.source.user_id, TextSendMessage(text=reply))
        except: pass

if __name__ == "__main__":
    app.run()
