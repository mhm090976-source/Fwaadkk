import hashlib
import json
import base64
import time
import datetime
import os
import httpx
import feedparser
import concurrent.futures
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# --- 1. الذاكرة المؤقتة للأخبار ---
NEWS_CACHE = {
    "data": [],
    "last_updated": 0
}

CACHE_DURATION = 7 * 60  # 7 دقائق

# --- 2. تهيئة Firebase الآمنة تماماً ---
db = None
firebase_status_msg = "Not initialized"

try:
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not firebase_admin._apps:
        cred_json = os.environ.get("FIREBASE_CREDENTIALS")
        if cred_json:
            try:
                cred_dict = json.loads(cred_json)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                firebase_status_msg = "Connected via FIREBASE_CREDENTIALS env var"
            except Exception as json_err:
                firebase_status_msg = f"JSON parse error: {str(json_err)}"
        else:
            try:
                firebase_admin.initialize_app()
                firebase_status_msg = "Connected via default app"
            except Exception as def_err:
                firebase_status_msg = f"Default init error: {str(def_err)}"

    if firebase_admin._apps:
        db = firestore.client()
except Exception as global_fb_err:
    firebase_status_msg = f"Firebase module error: {str(global_fb_err)}"

print("Firebase Status:", firebase_status_msg)

# --- 3. Cryptomus Verification ---
@app.route("/cryptomus_b9c7b8dd.html", methods=["GET"])
def cryptomus_verification():
    return Response("cryptomus=b9c7b8dd", mimetype="text/plain")

# --- 4. جلب الأخبار ---
RSS_FEEDS = {
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "DailyFX": "https://www.dailyfx.com/feeds/market-news",
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex"
}

HIGH_IMPACT = ['fed', 'interest rate', 'cpi', 'nfp', 'powell', 'inflation', 'ecb', 'central bank', 'fomc', 'gdp']
MEDIUM_IMPACT = ['dollar', 'euro', 'pound', 'yen', 'usd', 'eur', 'gbp', 'jpy', 'trade', 'jobless', 'yield']

def get_impact_level(title):
    t = title.lower()
    if any(k in t for k in HIGH_IMPACT): return "high"
    if any(k in t for k in MEDIUM_IMPACT): return "medium"
    return "low"

def fetch_single_feed(source_name, feed_url):
    items = []
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:
            title = entry.get("title", "")
            items.append({
                "title": title,
                "link": entry.get("link", "#"),
                "published": entry.get("published", entry.get("updated", "recent")),
                "source": source_name,
                "image": entry.get("media_content", [{}])[0].get("url", "") if "media_content" in entry else "",
                "impact": get_impact_level(title),
                "target_timestamp": None
            })
    except Exception:
        pass
    return items

def refresh_news():
    global NEWS_CACHE
    all_news = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_single_feed, name, url) for name, url in RSS_FEEDS.items()]
        for future in concurrent.futures.as_completed(futures):
            all_news.extend(future.result())

    if all_news:
        NEWS_CACHE["data"] = all_news
        NEWS_CACHE["last_updated"] = time.time()

# --- 5. المسارات الرئيسية ---
@app.route("/")
def home():
    articles = []
    if db:
        try:
            articles_ref = db.collection('articles').stream()
            articles = [doc.to_dict() for doc in articles_ref]
        except Exception as e:
            print("Error reading Firestore:", e)

    return jsonify({
        "status": "live",
        "firebase_connected": db is not None,
        "firebase_status": firebase_status_msg,
        "database_articles": articles
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
