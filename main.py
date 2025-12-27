import os
import json
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
import google.generativeai as genai
from groq import Groq
import yt_dlp

app = Flask(__name__)

# 從環境變數讀取鑰匙
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

        # --- 策略 A: 超強效字幕抓取 (修正版) ---
        try:
            print(f"嘗試抓取字幕: {video_id}")
            # 1. 取得該影片所有可用的字幕列表
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            
            # 2. 嘗試搜尋中文 (各種變體) 或 英文
            # 這裡會自動包含「自動產生」的字幕，不會因為沒人手打字幕就失敗
            try:
                transcript = transcript_list.find_transcript(['zh-TW', 'zh-Hant', 'zh', 'zh-CN', 'zh-Hans', 'en'])
            except:
                # 3. 如果真的都沒有，就抓「列表中的第一個」 (不管哪國語言，先抓再說)
                print("找不到指定語言，嘗試抓取任意可用字幕...")
                transcript = list(transcript_list)[0]

            # 4. 下載並格式化
            text_data = transcript.fetch()
            full_text = " ".join([i['text'] for i in text_data])
            
            return "CC字幕", full_text

        except Exception as e:
            print(f"字幕抓取完全失敗，原因: {e}")
            # 只有在真的連「自動產生字幕」都沒有時，才進入策略 B

        # --- 策略 B: 語音轉錄 (僅當作最後手段) ---
        # 注意：在免費雲端主機上，這一步容易被 YouTube 阻擋 (HTTP 429/Sign in required)
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio', 
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'noplaylist': True,
            # 嘗試偽裝成瀏覽器 User Agent 以降低被擋機率
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
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
        
        if os.path.exists(filename):
            os.remove(filename)
            
        return "語音轉錄", transcription

    except Exception as e:
        return "錯誤", f"無法處理此影片 (可能無字幕且 YouTube 阻擋下載): {str(e)}"

# 核心功能：寫文章
def summarize_text(text):
    model = genai.GenerativeModel('gemini-1.5-flash')
    # 這裡稍微修改 Prompt，讓 AI 知道如果是英文或亂碼要翻譯
    prompt = f"""
    你是一位專業主編。請閱讀以下影片逐字稿（可能是語音辨識結果或自動字幕），並用「繁體中文」寫成一篇重點懶人包。
    
    要求：
    1. 標題要吸睛。
    2. 結構包含：【前言】、【核心重點摘要】(條列式)、【結論】。
    3. 若原文是外語，請直接翻譯並整合。
    
    內容：
    {text[:25000]}
    """
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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！正在分析影片 (約需 15~30 秒)..."))
        
        source, content = get_video_content(msg)
        
        if source == "錯誤":
            reply = f"❌ 失敗: {content}"
        else:
            summary = summarize_text(content)
            reply = f"✅ 完成 (來源: {source})\n\n{summary}"
        
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=reply))
        except Exception as e:
            print(f"Push error: {e}")

if __name__ == "__main__":
    app.run()
