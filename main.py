import hashlib
import json
import base64
import time
import datetime
import os
import httpx
import feedparser
import concurrent.futures
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import firebase_admin
from firebase_admin import firestore

app = Flask(__name__)
CORS(app)

# --- 1. تهيئة الذاكرة المؤقتة (Cache) ---
# ستحفظ الأخبار ووقت آخر تحديث
NEWS_CACHE = {
    "data": [],
    "last_updated": 0
}

CACHE_DURATION_SECONDS = 7 * 60  # 7 دقائق

# --- 2. تهيئة Firebase ---
db = None
try:
    firebase_admin.initialize_app()
    db = firestore.client()
    print("Firebase initialized successfully.")
except Exception as e:
    print("Firebase initialization skipped or failed:", e)

# --- 3. إعدادات Cryptomus ---
CRYPTOMUS_MERCHANT_ID = "b9c7b8dd-cc24-4c13-beef-1f97ae33f932"
CRYPTOMUS_PAYMENT_KEY = "YOUR_PAYMENT_API_KEY_HERE"

@app.route("/cryptomus_b9c7b8dd.html", methods=["GET"])
def cryptomus_verification():
    return Response("cryptomus=b9c7b8dd", mimetype="text/plain")

def generate_cryptomus_signature(data_dict: dict, api_key: str) -> str:
    json_data = json.dumps(data_dict, separators=(',', ':')).encode('utf-8')
    base64_data = base64.b64encode(json_data).decode('utf-8')
    sign_str = base64_data + api_key
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

@app.route("/api/create-invoice", methods=["POST"])
def create_subscription_invoice():
    if CRYPTOMUS_PAYMENT_KEY == "YOUR_PAYMENT_API_KEY_HERE":
        return jsonify({"status": "error", "message": "Cryptomus Payment Key is missing"}), 400

    req_data = request.json or {}
    amount = req_data.get("amount", "10.00")
    order_id = req_data.get("order_id", "SUB_1001")

    payload = {
        "amount": amount,
        "currency": "USD",
        "order_id": order_id,
        "network": "TRON",
        "to_currency": "USDT",
        "url_callback": "https://fwaadkk.onrender.com/api/cryptomus-webhook",
        "url_success": "https://fwaadkk.onrender.com/?payment=success",
        "is_payment_multiple": False,
        "lifetime": 3600
    }

    signature = generate_cryptomus_signature(payload, CRYPTOMUS_PAYMENT_KEY)

    headers = {
        "merchant": CRYPTOMUS_MERCHANT_ID,
        "sign": signature,
        "Content-Type": "application/json"
    }

    try:
        with httpx.Client() as client:
            response = client.post(
                "https://api.cryptomus.com/v1/payment",
                json=payload,
                headers=headers,
                timeout=10.0
            )

        res_data = response.json()
        if response.status_code == 200 and res_data.get("state") == 0:
            return jsonify({
                "status": "success",
                "payment_url": res_data["result"]["url"],
                "invoice_id": res_data["result"]["uuid"]
            })
        else:
            return jsonify({"status": "error", "message": res_data.get("message", "Failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/cryptomus-webhook", methods=["POST"])
def cryptomus_webhook():
    try:
        data = request.json or {}
        status = data.get("status")

        if status in ["paid", "paid_over"]:
            return jsonify({"status": "ok"})

        return jsonify({"status": "ignored"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# --- 4. إعدادات جلب الأخبار ---
RSS_FEEDS = {
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "Investing.com Central Banks": "https://www.investing.com/rss/news_14.rss",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "DailyFX": "https://www.dailyfx.com/feeds/market-news",
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "Yahoo Finance Currency": "https://finance.yahoo.com/news/rssindex"
}

HIGH_IMPACT = ['fed', 'interest rate', 'cpi', 'nfp', 'powell', 'inflation', 'ecb', 'central bank', 'fomc', 'gdp']
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

def fetch_single_feed(source_name, feed_url):
    items = []
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:
            title = entry.get("title", "")
            if is_forex_news(title):
                image_url = ""
                if "media_content" in entry and len(entry.media_content) > 0:
                    image_url = entry.media_content[0].get("url", "")
                elif "enclosures" in entry and len(entry.enclosures) > 0:
                    image_url = entry.enclosures[0].get("href", "")

                items.append({
                    "title": title,
                    "link": entry.get("link", "#"),
                    "published": entry.get("published", entry.get("updated", "recent")),
                    "source": source_name,
                    "image": image_url,
                    "impact": get_impact_level(title),
                    "target_timestamp": None
                })
    except Exception:
        pass
    return items

# --- 5. دالة تحديث الأخبار في الخلفية تلقائياً ---
def update_news_cache_job():
    global NEWS_CACHE
    all_news = []
    current_time_ms = int(time.time() * 1000)

    # أ) جلب تقويم Forex Factory
    try:
        with httpx.Client() as client:
            cal_res = client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=4.0)
            if cal_res.status_code == 200:
                events = cal_res.json()
                for ev in events:
                    if ev.get("impact") in ["High", "Medium"]:
                        try:
                            dt_str = ev.get("date", "").replace("Z", "+00:00")
                            dt = datetime.datetime.fromisoformat(dt_str)
                            event_timestamp = int(dt.timestamp() * 1000)

                            if event_timestamp > current_time_ms:
                                all_news.append({
                                    "title": f"{ev.get('country')} - {ev.get('title')}",
                                    "link": "https://www.forexfactory.com/calendar",
                                    "published": f"موعد الصدور: {ev.get('date')}",
                                    "source": "Forex Factory Calendar",
                                    "image": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=600&q=80",
                                    "impact": "high" if ev.get("impact") == "High" else "medium",
                                    "target_timestamp": event_timestamp
                                })
                        except Exception:
                            continue
    except Exception as e:
        print("Calendar fetch error:", e)

    # ب) جلب RSS بالتوازي
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_single_feed, name, url) for name, url in RSS_FEEDS.items()]
        for future in concurrent.futures.as_completed(futures):
            all_news.extend(future.result())

    # ج) تحديث الذاكرة المؤقتة
    if all_news:
        NEWS_CACHE["data"] = all_news
        NEWS_CACHE["last_updated"] = time.time()
        print(f"[{datetime.datetime.now()}] Cache Updated! Total items: {len(all_news)}")

# --- 6. تشغيل المجدول (Background Scheduler) كل 30 ثانية ---
scheduler = BackgroundScheduler()
scheduler.add_job(func=update_news_cache_job, trigger="interval", seconds=30, max_instances=2)
scheduler.start()

# تشغيل أول جلب عند بداية التشغيل
update_news_cache_job()

# --- 7. المسارات الرئيسية ---
@app.route("/", methods=["GET", "HEAD"])
def home():
    if request.method == "HEAD":
        return Response(status=200)

    news_list = []
    try:
        if db:
            articles_ref = db.collection('articles').stream()
            news_list = [doc.to_dict() for doc in articles_ref]
    except Exception as e:
        print("Firestore Fetch Error:", e)

    try:
        return render_template('index.html', news_list=news_list)
    except Exception:
        return jsonify({"status": "live", "message": "API Server is running successfully"})

@app.route("/api/news", methods=["GET"])
def get_forex_news():
    now = time.time()
    # إذا كانت البيانات أقدم من 7 دقائق ولا توجد بيانات، قم بالتحديث التلقائي
    if not NEWS_CACHE["data"] or (now - NEWS_CACHE["last_updated"] > CACHE_DURATION_SECONDS):
        update_news_cache_job()

    # إرجاع البيانات المحفوظة في الذاكرة فوراً (استجابة فائقة السرعة!)
    return jsonify({
        "status": "success",
        "total_results": len(NEWS_CACHE["data"]),
        "data": NEWS_CACHE["data"],
        "cached_at": NEWS_CACHE["last_updated"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
