import os
import json
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai
from groq import Groq
import yt_dlp

app = Flask(__name__)

# 從環境變數讀取鑰匙 (部署到 Render 時才設定)
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))
groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

# 核心功能：分析影片
def get_video_content(video_url):
    try:
        # 簡易抓取 Video ID
        if "v=" in video_url:
            video_id = video_url.split("v=")[-1].split("&")[0]
        elif "youtu.be" in video_url:
            video_id = video_url.split("/")[-1].split("?")[0]
        else:
            return "錯誤", "無法辨識網址"

        # 策略 1: 優先抓字幕 (速度快)
        try:
            print(f"嘗試抓字幕: {video_id}")
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['zh-TW', 'zh-Hant', 'en'])
            full_text = " ".join([i['text'] for i in transcript_list])
            return "CC字幕", full_text
        except:
            print("無字幕，切換至語音轉錄...")

        # 策略 2: 無字幕則下載音訊轉錄 (Groq Whisper)
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio', 
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'noplaylist': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            filename = ydl.prepare_filename(info)
        
        # 呼叫 Groq 轉錄
        with open(filename, "rb") as file:
            transcription = groq_client.audio.transcriptions.create(
                file=(filename, file.read()),
                model="whisper-large-v3",
                response_format="text"
            )
        
        # 刪除暫存檔
        if os.path.exists(filename):
            os.remove(filename)
            
        return "語音轉錄", transcription

    except Exception as e:
        return "錯誤", str(e)

# 核心功能：寫文章
def summarize_text(text):
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"請將以下逐字稿整理成一篇繁體中文的「重點懶人包」，包含標題、摘要與結論：\n\n{text[:25000]}"
    try:
        response = model.generate_content(prompt)
        return response.text
    except:
        return "AI 生成文章失敗，可能是內容過長或含有敏感詞。"

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
    msg = event.message.text
    user_id = event.source.user_id
    
    if "youtube.com" in msg or "youtu.be" in msg:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！正在分析影片，若無字幕需進行語音轉錄，請稍候約 1~2 分鐘..."))
        
        source, content = get_video_content(msg)
        
        if source == "錯誤":
            reply = f"❌ 失敗: {content}"
        else:
            summary = summarize_text(content)
            reply = f"✅ 完成 (來源: {source})\n\n{summary}"
        
        # 用 Push Message 主動推播結果
        line_bot_api.push_message(user_id, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()