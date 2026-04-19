import os
import re
import json
import feedparser
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import TextSendMessage
from linebot.exceptions import LineBotApiError
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load environment variables
load_dotenv()

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")
GCP_SERVICE_ACCOUNT_JSON = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

# RSS Feeds (AI related Japanese sources)
RSS_FEEDS = [
    "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",  # ITmedia AI+
    "https://ledge.ai/feed",                         # Ledge.ai
    "https://ainow.ai/feed",                         # AINOW
    "https://google.com/search?q=AI+%E3%83%8B%E3%83%A5%E3%83%BC%E3%82%B9&tbm=nws&output=rss", # Google News AI (JP)
]

# Japan timezone offset
JST = timezone(timedelta(hours=9))

# User-Agent for scraping
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_news():
    """Fetch news from RSS feeds published in the last 24 hours."""
    news_list = []
    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)

    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            published_parsed = getattr(entry, "published_parsed", None)
            if published_parsed:
                published_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                if published_dt > one_day_ago:
                    news_list.append({
                        "title": entry.title,
                        "link": entry.link,
                        "summary": entry.get("summary", ""),
                        "source": feed.feed.get("title", "Unknown Source")
                    })
    
    # Deduplicate by link
    seen_links = set()
    unique_news = []
    for news in news_list:
        if news["link"] not in seen_links:
            unique_news.append(news)
            seen_links.add(news["link"])
            
    return unique_news

def scrape_article_body(url):
    """Scrape the main body text from a news article URL."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")

        # Remove script, style, nav, header, footer, aside elements
        for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
            tag.decompose()

        # Try common article body selectors (prioritized for Japanese news sites)
        selectors = [
            "article",                          # Generic article tag
            ".article-body",                    # ITmedia
            ".article_body",                    # Common pattern
            "#article-body",                    # Common pattern
            ".entry-content",                   # WordPress-based (Ledge.ai, AINOW)
            ".post-content",                    # Blog-style
            ".content-main",                    # Generic
            "main",                             # Fallback to main tag
        ]

        body_text = ""
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                # Get text, collapse whitespace
                paragraphs = element.find_all(["p", "h2", "h3", "li"])
                if paragraphs:
                    body_text = "\n".join([p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)])
                else:
                    body_text = element.get_text(separator="\n", strip=True)
                break

        if not body_text:
            # Ultimate fallback: get all <p> tags from the page
            paragraphs = soup.find_all("p")
            body_text = "\n".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])

        # Limit to ~2000 chars per article to keep Doc size manageable
        if len(body_text) > 2000:
            body_text = body_text[:2000] + "...(以下省略)"

        return body_text if body_text else "(本文の取得に失敗しました)"

    except Exception as e:
        return f"(本文の取得中にエラーが発生: {str(e)})"

def fetch_article_bodies(news_list):
    """Fetch article body text for each news item."""
    print(f"Scraping article bodies for {len(news_list)} articles...")
    for i, news in enumerate(news_list):
        print(f"  [{i+1}/{len(news_list)}] {news['title'][:40]}...")
        news["body"] = scrape_article_body(news["link"])
    return news_list

def summarize_news(news_list):
    """Summarize the news list into a single message using Gemini."""
    if not news_list:
        return "本日のAI関連ニュースはありませんでした。"

    if not GEMINI_API_KEY:
        return "エラー: GEMINI_API_KEYが設定されていません。"

    genai.configure(api_key=GEMINI_API_KEY)
    
    # 物理的なタグ抽出方式を採用
    # Gemma 4がどれだけお喋りをしても、[[SUMMARY_START]] と [[SUMMARY_END]] の間だけを抜き出す
    system_instr = (
        "あなたは日本のAI技術専門のジャーナリストです。ニュースの内容を分析し、"
        "日本の読者が分かりやすいよう、簡潔な日本語で要約してください。\n\n"
        "【重要ルール】\n"
        "1. 要約の「前後に」必ず以下のタグを単独行で挿入してください。\n"
        "   - 要約の開始：[[SUMMARY_START]]\n"
        "   - 要約の終了：[[SUMMARY_END]]\n"
        "2. タグの外側でどれだけ思考プロセス（Role, Task, Wait...等）を英語や日本語で出力しても構いませんが、"
        "要約の本文そのものは必ず日本語で、指定した開始・終了タグの内側に出力してください。"
    )
    
    model = genai.GenerativeModel(
        model_name="gemma-4-31b-it",
        system_instruction=system_instr
    )

    content = "\n".join([f"- {n['title']} ({n['source']}): {n['link']}" for n in news_list])
    prompt = f"""以下のニュースリストから、重要なAI関連ニュースを3〜5個抽出し、日本語で要約してください。
要約の開始には [[SUMMARY_START]]、終了には [[SUMMARY_END]] を必ず付けてください。

[ニュースリスト]
{content}
"""

    try:
        response = model.generate_content(prompt)
        full_text = response.text
        
        # 正規表現で [[SUMMARY_START]] と [[SUMMARY_END]] の間を抽出
        pattern = r"\[\[SUMMARY_START\]\](.*?)\[\[SUMMARY_END\]\]"
        match = re.search(pattern, full_text, re.DOTALL)
        
        if match:
            summary_part = match.group(1).strip()
            # 万が一英語が残っていないか、最終的なクリーンアップ
            return summary_part
        else:
            # タグが見当たらない場合のフォールバック（従来通りテキスト全部を返すが、念のため警告）
            print("Warning: Tags not found in LLM response. Returning full text.")
            return full_text
    except Exception as e:
        return f"エラー: 要約の生成に失敗しました ({str(e)})"

def send_line_message(message):
    """Send a push message via LINE Messaging API."""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("LINE_CHANNEL_ACCESS_TOKEN or LINE_USER_ID is not set.")
        return

    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    try:
        line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=message))
        print("Notification sent successfully via Messaging API!")
    except LineBotApiError as e:
        print(f"Failed to send notification: {e.status_code}")
        print(e.message)

def get_weekly_doc_title():
    """Get the document title for the current week (Sunday-Saturday, wk = %U + 1)."""
    now = datetime.now(JST)
    week_number = int(now.strftime("%U")) + 1
    return f"Yuzuhiko AI News_wk{week_number}"

def get_google_credentials():
    """Get Google API credentials with Drive and Docs scopes."""
    creds_info = json.loads(GCP_SERVICE_ACCOUNT_JSON)
    return service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.file",
        ]
    )

def get_or_create_weekly_doc(creds):
    """Find or create this week's Google Doc in the specified folder."""
    drive_service = build("drive", "v3", credentials=creds)
    doc_title = get_weekly_doc_title()

    # Search for existing doc with this week's title in the target folder
    query = (
        f"name = '{doc_title}' "
        f"and '{GOOGLE_DRIVE_FOLDER_ID}' in parents "
        f"and mimeType = 'application/vnd.google-apps.document' "
        f"and trashed = false"
    )
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        doc_id = files[0]["id"]
        print(f"Found existing weekly doc: {doc_title} (ID: {doc_id})")
        return doc_id

    # Create a new doc in the folder
    file_metadata = {
        "name": doc_title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [GOOGLE_DRIVE_FOLDER_ID],
    }
    created_file = drive_service.files().create(
        body=file_metadata, fields="id"
    ).execute()
    doc_id = created_file["id"]
    print(f"Created new weekly doc: {doc_title} (ID: {doc_id})")
    return doc_id

def append_to_google_doc(news_list, summary):
    """Append today's news summary and article bodies to this week's Google Document."""
    if not GCP_SERVICE_ACCOUNT_JSON or not GOOGLE_DRIVE_FOLDER_ID:
        print("Google Docs credentials or Folder ID not set. Skipping Google Docs append.")
        return

    try:
        creds = get_google_credentials()
        doc_id = get_or_create_weekly_doc(creds)
        docs_service = build("docs", "v1", credentials=creds)

        # Build the text to append
        today = datetime.now(JST).strftime("%Y年%m月%d日")
        separator = "=" * 50
        
        text_to_append = f"\n\n{separator}\n"
        text_to_append += f"📅 {today} のAIニュースまとめ\n"
        text_to_append += f"{separator}\n\n"
        text_to_append += f"【要約】\n{summary}\n\n"
        
        # Append full article bodies for NotebookLM
        text_to_append += f"{'─' * 50}\n"
        text_to_append += "【各記事の本文（NotebookLM用）】\n"
        text_to_append += f"{'─' * 50}\n\n"
        
        for n in news_list:
            text_to_append += f"■ {n['title']}\n"
            text_to_append += f"  出典: {n['source']}\n"
            text_to_append += f"  URL: {n['link']}\n\n"
            body = n.get("body", "(本文なし)")
            text_to_append += f"{body}\n\n"
            text_to_append += f"{'- ' * 25}\n\n"

        # Get the current document length to append at the end
        doc = docs_service.documents().get(documentId=doc_id).execute()
        end_index = doc["body"]["content"][-1]["endIndex"] - 1

        requests_body = [
            {
                "insertText": {
                    "location": {"index": end_index},
                    "text": text_to_append
                }
            }
        ]

        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests_body}
        ).execute()

        print(f"Successfully appended news to weekly doc: {get_weekly_doc_title()} (ID: {doc_id})")

    except Exception as e:
        print(f"Failed to append to Google Doc: {str(e)}")

def main():
    print("Fetching news...")
    news = fetch_news()
    print(f"Found {len(news)} new articles.")
    
    print("Scraping article bodies...")
    news = fetch_article_bodies(news)
    
    print("Summarizing news...")
    summary = summarize_news(news)
    
    print("Sending LINE notification...")
    line_summary = summary
    if len(line_summary) > 4900:
        line_summary = line_summary[:4900] + "..."
    send_line_message(line_summary)

    print("Appending to Google Docs (with full article text)...")
    append_to_google_doc(news, summary)

    print("Done!")

if __name__ == "__main__":
    main()
