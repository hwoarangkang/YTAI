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

# --- 1. 設定 API 金鑰 (從 Render 環境變數讀取) ---
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))
groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

# --- 2. 設定 Gemini 安全過濾 (定義在這裡，避免報錯) ---
# 設定為 BLOCK_NONE 可以防止 AI 因為誤判內容敏感而拒絕生成
safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# --- 3. 核心功能：抓取影片內容 ---
def get_video_content(video_url):
    print(f"收到影片連結: {video_url}")
    try:
        # 簡易抓取 Video ID
        if "v=" in video_url:
            video_id = video_url.split("v=")[-1].split("&")[0]
        elif "youtu.be" in video_url:
            video_id = video_url.split("/")[-1].split("?")[0]
        else:
            return "錯誤", "無法辨識 YouTube 網址格式"

        # [策略 A] 嘗試抓取 CC 字幕 (最快)
        try:
            print("正在嘗試抓取字幕...")
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['zh-TW', 'zh-Hant', 'en', 'ja'])
            full_text = " ".join([i['text'] for i in transcript_list])
            return "CC字幕", full_text
        except Exception as e:
            print(f"抓取字幕失敗 ({e})，準備切換至語音轉錄...")

        # [策略 B] 下載音訊並用 Groq Whisper 轉錄 (備援)
        print("啟動 yt-dlp 下載音訊...")
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio', 
            'outtmpl': '/tmp/%(id)s.%(ext)s',  # Render 的暫存資料夾
            'noplaylist': True,
            'quiet': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            filename = ydl.prepare_filename(info)
        
        print(f"音訊下載完成: {filename}，開始上傳 Groq...")
        
        with open(filename, "rb") as file:
            transcription = groq_client.audio.transcriptions.create(
                file=(filename, file.read()),
                model="whisper-large-v3",
                response_format="text"
            )
        
        # 刪除暫存檔以節省空間
        if os.path.exists(filename):
            os.remove(filename)
            
        return "語音轉錄(Groq)", transcription

    except Exception as e:
        return "錯誤", f"系統處理失敗: {str(e)}"

# --- 4. 核心功能：AI 生成文章 ---
def summarize_text(text):
    try:
        # 使用 Flash 模型速度較快
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # 限制字數以免超過 Token 上限 (擷取前 30000 字)
        input_text = text[:30000] 
        
        prompt = f"""
        你是一位專業的主編。請根據以下影片逐字稿，撰寫一篇繁體中文的「重點懶人包」。
        
        【要求】
        1. 標題：要吸引人。
        2. 結構：前言、核心重點（請用條列式）、結論。
        3. 語氣：通順流暢，去除口語贅字。
        
        【逐字稿內容】
        {input_text}
        """
        
        # 這裡引用了上方定義好的 safety_settings
        response = model.generate_content(prompt, safety_settings=safety_settings)
        return response.text
    except Exception as e:
        return f"AI 生成失敗: {str(e)}"

# --- 5. LINE Webhook 入口 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# --- 6. 訊息處理邏輯 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id
    
    # 檢查是否為 YouTube 連結
    if "youtube.com" in msg or "youtu.be" in msg:
        # 先回覆「處理中」避免使用者以為沒反應
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到影片！正在分析內容，若無字幕需進行語音轉錄，可能耗時 1~2 分鐘，請稍候..."))
        except:
            pass # 如果 reply token 失效就算了

        # 開始處理 (耗時操作)
        source_type, content = get_video_content(msg)
        
        if source_type == "錯誤":
            result_msg = f"❌ 分析失敗：\n{content}"
        else:
            summary = summarize_text(content)
            result_msg = f"✅ 分析完成 (來源：{source_type})\n\n{summary}"
        
        # 使用 Push Message 主動推播結果 (避免 Reply Token 過期)
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=result_msg))
        except Exception as e:
            print(f"推播失敗: {e}")

if __name__ == "__main__":
    # Render 會預設使用 PORT 環境變數
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
