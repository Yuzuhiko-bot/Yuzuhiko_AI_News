import os
import feedparser
import google.generativeai as genai
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import TextSendMessage
from linebot.exceptions import LineBotApiError

# Load environment variables
load_dotenv()

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

# RSS Feeds (AI related Japanese sources)
RSS_FEEDS = [
    "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",  # ITmedia AI+
    "https://ledge.ai/feed",                         # Ledge.ai
    "https://ainow.ai/feed",                         # AINOW
    "https://google.com/search?q=AI+%E3%83%8B%E3%83%A5%E3%83%BC%E3%82%B9&tbm=nws&output=rss", # Google News AI (JP)
]

def fetch_news():
    """Fetch news from RSS feeds published in the last 24 hours."""
    news_list = []
    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)

    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            # Parse publication date
            # Different feeds might use different date formats, feedparser handles most
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

def summarize_news(news_list):
    """Summarize the news list into a single message using Gemini."""
    if not news_list:
        return "本日のAI関連ニュースはありませんでした。"

    if not GEMINI_API_KEY:
        return "エラー: GEMINI_API_KEYが設定されていません。"

    genai.configure(api_key=GEMINI_API_KEY)
    # Using gemini-2.5-flash as specified by the user.
    model = genai.GenerativeModel("gemini-2.5-flash")

    # Construct the prompt
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

def main():
    print("Fetching news...")
    news = fetch_news()
    print(f"Found {len(news)} new articles.")
    
    print("Summarizing news...")
    summary = summarize_news(news)
    
    print("Sending notification...")
    # Messaging API has a 5000 char limit for text messages
    if len(summary) > 4900:
        summary = summary[:4900] + "..."
    
    send_line_message(summary)

if __name__ == "__main__":
    main()
