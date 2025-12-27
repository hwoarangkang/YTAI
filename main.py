import os
import re
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai
from groq import Groq
import yt_dlp

app = Flask(__name__)

# 從環境變數讀取鑰匙
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))
groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY'))

# --- 新增功能：替身攻擊 (透過 Piped API 繞過封鎖) ---
def get_transcript_via_proxy(video_id):
    try:
        print(f"啟動替身模式 (Piped API) 分析: {video_id}")
        # 使用 Piped API 抓取字幕列表
        url = f"https://pipedapi.kavin.rocks/streams/{video_id}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        subtitles = data.get('subtitles', [])
        target_sub = None
        
        # 1. 優先找繁體/正體中文
        for sub in subtitles:
            if sub.get('code') in ['zh-TW', 'zh-Hant']:
                target_sub = sub
                break
        
        # 2. 其次找通用中文
        if not target_sub:
            for sub in subtitles:
                if 'zh' in sub.get('code', ''):
                    target_sub = sub
                    break
                    
        # 3. 再不行找英文
        if not target_sub:
            for sub in subtitles:
                if 'en' in sub.get('code', ''):
                    target_sub = sub
                    break
        
        # 4. 真的沒有就抓第一個 (包含自動產生)
        if not target_sub and subtitles:
            target_sub = subtitles[0]
            
        if target_sub:
            # 下載字幕內容
            print(f"找到字幕: {target_sub['name']} ({target_sub['code']})")
            sub_text = requests.get(target_sub['url']).text
            # 簡單清理 VTT 格式的時間軸 (讓 AI 比較好讀)
            clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', '', sub_text)
            clean_text = re.sub(r'<[^>]+>', '', clean_text) # 去除 HTML 標籤
            return clean_text
            
        return None
    except Exception as e:
        print(f"替身模式失敗: {e}")
        return None

# 核心功能：分析影片
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

        # --- 第一關：正規軍 (youtube-transcript-api) ---
        try:
            print(f"嘗試正規字幕抓取: {video_id}")
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = transcript_list.find_transcript(['zh-TW', 'zh-Hant', 'zh', 'zh-CN', 'en'])
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(直連)"
        except Exception:
            print("正規抓取失敗，準備切換替身模式...")

        # --- 第二關：替身攻擊 (Piped API) ---
        # 如果第一關失敗 (被封鎖或沒字幕)，走這條路
        if not full_text:
            proxy_text = get_transcript_via_proxy(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"

        # --- 第三關：語音轉錄 (最後手段) ---
        # 如果連替身都抓不到字幕 (代表真的沒字幕)，才冒險用下載的
        if not full_text:
            try:
                print("無字幕，嘗試語音轉錄...")
                # 這裡可能會失敗，但已經是最後一招了
                ydl_opts = {
                    'format': 'bestaudio[ext=m4a]/bestaudio', 
                    'outtmpl': '/tmp/%(id)s.%(ext)s',
                    'noplaylist': True,
                    # 偽裝 User Agent
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
                source_type = "語音轉錄"
            except Exception as e:
                return "錯誤", f"所有方法都失敗 (IP 被封鎖且無替代字幕): {str(e)}"

        return source_type, full_text

    except Exception as e:
        return "錯誤", str(e)

# 核心功能：寫文章
def summarize_text(text):
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"""
    你是一位專業主編。請閱讀以下影片內容（可能包含時間軸雜訊），並用「繁體中文」寫成一篇重點懶人包。
    
    要求：
    1. 標題要吸睛。
    2. 結構包含：【前言】、【核心重點摘要】(條列式)、【結論】。
    3. 若原文是外語，請直接翻譯。
    4. 忽略內容中的時間碼 (如 00:01:23)。
    
    內容：
    {text[:30000]}
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except:
        return "AI 生成文章失敗 (內容過長或敏感)。"

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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！正在啟動多重路徑分析影片..."))
        
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
