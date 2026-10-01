import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import feedparser

app = FastAPI(title="Forex News Aggregator - Top 20 Web Sources")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FOREX_SOURCES = [
    {"name": "FXStreet", "url": "https://www.fxstreet.com/rss/news"},
    {"name": "DailyFX", "url": "https://www.dailyfx.com/feeds/forex-market-news"},
    {"name": "Investing.com", "url": "https://www.investing.com/rss/news_25.rss"},
    {"name": "ForexLive", "url": "http://feeds.feedburner.com/forexlive"},
    {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"name": "Myfxbook", "url": "https://www.myfxbook.com/rss/forex-economic-calendar-events"},
    {"name": "Reuters Money", "url": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best"},
    {"name": "CNBC Markets", "url": "https://search.cnbc.com/rs/search/combinedrenderer.view?query=forex&partnerId=2000&target=news"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "FX Empire", "url": "https://www.fxempire.com/api/v1/en/news/rss"},
    {"name": "Action Forex", "url": "https://www.actionforex.com/feed/"},
    {"name": "Trading Economics", "url": "https://tradingeconomics.com/rss/news.aspx"},
    {"name": "FXStreet Analysis", "url": "https://www.fxstreet.com/rss/analysis"},
    {"name": "Investing Forex", "url": "https://www.investing.com/rss/forex.rss"},
    {"name": "Forex Crunch", "url": "https://www.forexcrunch.com/feed/"},
    {"name": "Seeking Alpha Macro", "url": "https://seekingalpha.com/tag/macro.xml"},
    {"name": "Financial Times", "url": "https://www.ft.com/currencies?format=rss"},
    {"name": "Central Banking News", "url": "https://www.centralbanking.com/rss/news"},
    {"name": "Bloomberg Markets", "url": "https://www.bloomberg.com/feed/podcast/mastermind.xml"},
    {"name": "Economy.com", "url": "https://www.economy.com/rss/global-overview.xml"}
]

latest_news = []

def fetch_all_20_sources():
    global latest_news
    temp_news = []
    for source in FOREX_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            for entry in feed.entries[:2]: 
                item = {
                    "source": source["name"],
                    "title": entry.title,
                    "link": entry.link,
                    "published": getattr(entry, 'published', 'الآن')
                }
                temp_news.append(item)
        except Exception as e:
            continue
    latest_news = temp_news

async def news_scanner_loop():
    while True:
        fetch_all_20_sources()
        await asyncio.sleep(15)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(news_scanner_loop())

@app.get("/api/news")
def get_news():
    return {
        "status": "success",
        "total": len(latest_news),
        "data": latest_news
    }
