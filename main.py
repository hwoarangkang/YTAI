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
import yt_dlp # 雖然被擋，但留著引用避免報錯

app = Flask(__name__)

# 從環境變數讀取鑰匙
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))

# 定義多個 Piped 替身伺服器 (影分身列表)
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.privacy.com.de",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.tokhmi.xyz",
    "https://pipedapi.moomoo.me",
    "https://api.piped.yt"
]

def get_transcript_via_piped(video_id):
    # 輪詢所有替身伺服器
    for instance in PIPED_INSTANCES:
        try:
            print(f"正在嘗試替身伺服器: {instance} ...")
            url = f"{instance}/streams/{video_id}"
            response = requests.get(url, timeout=5) # 設定 5 秒超時，不行就換下一個
            
            if response.status_code != 200:
                continue # 失敗，換下一個
                
            data = response.json()
            subtitles = data.get('subtitles', [])
            
            if not subtitles:
                continue # 沒字幕，換下一個

            target_sub = None
            # 1. 優先找繁體/正體中文
            for sub in subtitles:
                if sub.get('code') in ['zh-TW', 'zh-Hant']:
                    target_sub = sub
                    break
            
            # 2. 其次找通用中文 (zh, zh-CN)
            if not target_sub:
                for sub in subtitles:
                    if 'zh' in sub.get('code', ''):
                        target_sub = sub
                        break
            
            # 3. 找英文
            if not target_sub:
                for sub in subtitles:
                    if 'en' in sub.get('code', ''):
                        target_sub = sub
                        break

            # 4. 隨便抓一個 (如果有自動產生 auto-generated)
            if not target_sub and subtitles:
                target_sub = subtitles[0]

            if target_sub:
                print(f"成功從 {instance} 抓到字幕: {target_sub['name']}")
                # 下載字幕文字
                sub_text = requests.get(target_sub['url']).text
                
                # 清理 VTT/XML 雜訊 (簡易版)
                # 移除時間軸
                clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', '', sub_text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text) 
                clean_text = re.sub(r'WEBVTT', '', clean_text)
                clean_text = re.sub(r'Kind: captions', '', clean_text)
                clean_text = re.sub(r'Language: .*', '', clean_text)
                
                # 合併成一行，去除多餘空行
                lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
                # 去除重複行 (歌詞或字幕常有重複)
                seen = set()
                final_lines = []
                for line in lines:
                    if line not in seen:
                        final_lines.append(line)
                        seen.add(line)
                
                return " ".join(final_lines)

        except Exception as e:
            print(f"{instance} 連線失敗: {e}")
            continue # 發生錯誤，換下一個
            
    return None

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

        # --- 策略 A: 正規軍 (試試運氣) ---
        try:
            print("策略 A: 正規抓取")
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            # 只要能抓到任何字幕都好
            transcript = list(transcript_list)[0] 
            full_text = " ".join([i['text'] for i in transcript.fetch()])
            source_type = "CC字幕(官方)"
        except Exception:
            print("正規抓取失敗 (預料中)，切換策略 B...")

        # --- 策略 B: 影分身之術 (多重 Piped 輪詢) ---
        if not full_text:
            print("策略 B: 啟動多重替身輪詢")
            proxy_text = get_transcript_via_piped(video_id)
            if proxy_text:
                full_text = proxy_text
                source_type = "CC字幕(替身)"
            else:
                return "失敗", "所有字幕伺服器都忙碌中，或該影片完全沒有字幕 (包含自動產生)。"

        return source_type, full_text

    except Exception as e:
        return "錯誤", str(e)

def summarize_text(text):
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"""
    你是一位專業主編。請閱讀以下影片字幕內容（可能包含雜訊），並用「繁體中文」寫成一篇重點懶人包。
    
    要求：
    1. 標題要吸睛。
    2. 結構包含：【前言】、【核心重點摘要】(條列式)、【結論】。
    3. 若原文是外語，請直接翻譯。
    4. 內容可能有些許亂碼，請自行推斷語意。
    
    內容：
    {text[:25000]}
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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🤖 收到！正在透過多重伺服器分析影片，請稍候..."))
        
        source, content = get_video_content(msg)
        
        if source == "失敗" or source == "錯誤":
            reply = f"❌ {content}"
        else:
            summary = summarize_text(content)
            reply = f"✅ 分析完成 (來源: {source})\n\n{summary}"
        
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=reply))
        except Exception as e:
            print(f"Push error: {e}")

if __name__ == "__main__":
    app.run()
