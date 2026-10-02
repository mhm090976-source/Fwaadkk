import os
import time
import httpx
import feedparser
import concurrent.futures
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

NEWS_CACHE = {
    "data": [],
    "last_updated": 0
}

CACHE_DURATION = 7 * 60

RSS_FEEDS = {
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "DailyFX": "https://www.dailyfx.com/feeds/market-news",
    "FXStreet": "https://www.fxstreet.com/rss/news"
}

HIGH_IMPACT = ['fed', 'interest rate', 'cpi', 'nfp', 'powell', 'inflation']

def get_impact_level(title):
    t = title.lower()
    if any(k in t for k in HIGH_IMPACT):
        return "high"
    return "medium"

def fetch_single_feed(source_name, feed_url):
    items = []
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:
            title = entry.get("title", "")
            items.append({
                "title": title,
                "link": entry.get("link", "#"),
                "published": entry.get("published", "recent"),
                "source": source_name,
                "image": "",
                "impact": get_impact_level(title),
                "target_timestamp": None
            })
    except Exception:
        pass
    return items

def refresh_news():
    global NEWS_CACHE
    all_news = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_single_feed, name, url) for name, url in RSS_FEEDS.items()]
        for future in concurrent.futures.as_completed(futures):
            all_news.extend(future.result())

    NEWS_CACHE["data"] = all_news
    NEWS_CACHE["last_updated"] = time.time()

@app.route("/")
def home():
    return jsonify({
        "status": "live",
        "message": "Server is running smoothly"
    })

@app.route("/api/news", methods=["GET"])
def get_forex_news():
    now = time.time()
    if not NEWS_CACHE["data"] or (now - NEWS_CACHE["last_updated"] > CACHE_DURATION):
        refresh_news()

    return jsonify({
        "status": "success",
        "total_results": len(NEWS_CACHE["data"]),
        "data": NEWS_CACHE["data"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
