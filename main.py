from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
import feedparser

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- مسار توثيق ملكية الدومين لـ Cryptomus ---
@app.get("/cryptomus_b9c7b8dd.html", response_class=PlainTextResponse)
def cryptomus_verification():
    return "cryptomus=b9c7b8dd"


RSS_FEEDS = {
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "Investing.com Central Banks": "https://www.investing.com/rss/news_14.rss",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "DailyFX": "https://www.dailyfx.com/feeds/market-news",
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "Yahoo Finance Currency": "https://finance.yahoo.com/news/rssindex"
}

# كلمات مفتاحية للأخبار عالية التأثير جداً
HIGH_IMPACT = ['fed', 'interest rate', 'cpi', 'nfp', 'powell', 'inflation', 'ecb', 'central bank', 'fomc', 'gdp']
# كلمات مفتاحية للأخبار متوسطة التأثير
MEDIUM_IMPACT = ['dollar', 'euro', 'pound', 'yen', 'usd', 'eur', 'gbp', 'jpy', 'trade', 'retail', 'jobless', 'treasury', 'yield']

def get_impact_level(title):
    title_lower = title.lower()
    if any(k in title_lower for k in HIGH_IMPACT):
        return "high"
    elif any(k in title_lower for k in MEDIUM_IMPACT):
        return "medium"
    return "low"

def is_forex_news(title):
    title_lower = title.lower()
    return any(k in title_lower for k in (HIGH_IMPACT + MEDIUM_IMPACT + ['forex', 'fx', 'currency']))

@app.get("/")
def home():
    return {"status": "online", "message": "Forex News High-Impact API is running"}

@app.get("/api/news")
def get_forex_news():
    all_news = []
    
    for source_name, feed_url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "")
                
                if is_forex_news(title):
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
                        "image": image_url,
                        "impact": get_impact_level(title) # تحديد الأهمية
                    })
        except Exception:
            continue

    return {
        "status": "success",
        "total_results": len(all_news),
        "data": all_news
    }
