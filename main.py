import os
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
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")

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
    model = genai.GenerativeModel("gemini-2.5-flash")

    content = "\n".join([f"- {n['title']} ({n['source']}): {n['link']}" for n in news_list])
    prompt = f"""以下のAI関連のニュース記事リストを、日本語で要約してください。
読者が手短に内容を把握できるように、重要なニュースを3〜5個に絞って簡潔に記述してください。
各要約の後に、該当記事のURLを記載してください。

ニュースリスト:
{content}
"""

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"要約中にエラーが発生しました: {str(e)}"

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

def append_to_google_doc(news_list, summary):
    """Append today's news summary and article bodies to a Google Document."""
    if not GCP_SERVICE_ACCOUNT_JSON or not GOOGLE_DOC_ID:
        print("Google Docs credentials or Doc ID not set. Skipping Google Docs append.")
        return

    try:
        creds_info = json.loads(GCP_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/documents"]
        )
        service = build("docs", "v1", credentials=creds)

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
        doc = service.documents().get(documentId=GOOGLE_DOC_ID).execute()
        end_index = doc["body"]["content"][-1]["endIndex"] - 1

        requests_body = [
            {
                "insertText": {
                    "location": {"index": end_index},
                    "text": text_to_append
                }
            }
        ]

        service.documents().batchUpdate(
            documentId=GOOGLE_DOC_ID,
            body={"requests": requests_body}
        ).execute()

        print(f"Successfully appended news with article bodies to Google Doc: {GOOGLE_DOC_ID}")

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
