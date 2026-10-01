from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import feedparser

app = FastAPI()

# تفعيل CORS لتسمح للواجهة بالاتصال بالسيرفر
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# قائمة مصادر RSS
RSS_FEEDS = {
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "Investing.com Central Banks": "https://www.investing.com/rss/news_14.rss",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "DailyFX": "https://www.dailyfx.com/feeds/market-news",
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "Yahoo Finance Currency": "https://finance.yahoo.com/news/rssindex"
}

# الكلمات المفتاحية المخصصة لتصفية أخبار الفوركس والاقتصاد
FOREX_KEYWORDS = [
    'forex', 'fx', 'dollar', 'euro', 'pound', 'yen', 'usd', 'eur', 'gbp', 'jpy', 'aud', 'cad', 'chf',
    'fed', 'ecb', 'boe', 'boj', 'inflation', 'interest rate', 'central bank', 'cpi', 'nfp', 'currency',
    'trade', 'treasury', 'yield', 'powell', 'lagarde', 'monetary'
]

def is_forex_news(title):
    """تصفية الأخبار بناءً على الكلمات المفتاحية"""
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in FOREX_KEYWORDS)

@app.get("/")
def home():
    return {"status": "online", "message": "Forex News API is running"}

@app.get("/api/news")
def get_forex_news():
    all_news = []
    
    for source_name, feed_url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                
                if is_forex_news(title):
                    # استخراج رابط الصورة إن وجد في الـ RSS
                    image_url = ""
                    if "media_content" in entry and len(entry.media_content) > 0:
                        image_url = entry.media_content[0].get("url", "")
                    elif "enclosures" in entry and len(entry.enclosures) > 0:
                        image_url = entry.enclosures[0].get("href", "")

                    all_news.append({
                        "title": title,
                        "link": entry.get("link", "#"),
                        "published": entry.get("published", entry.get("updated", "مؤخراً")),
                        "source": source_name,
                        "image": image_url
                    })
        except Exception:
            continue

    return {
        "status": "success",
        "total_results": len(all_news),
        "data": all_news
    }
