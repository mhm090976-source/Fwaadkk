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
from firebase_admin import firestore, messaging

app = Flask(__name__)
CORS(app)

# --- 1. تهيئة الذاكرة المؤقتة (Cache) ---
NEWS_CACHE = {
    "data": [],
    "last_updated": 0
}

# تتبع الأخبار المُنبه عليها سابقاً
NOTIFIED_NEWS_TITLES = set()

# --- 2. تهيئة Firebase ---
db = None
try:
    firebase_admin.initialize_app()
    db = firestore.client()
    print("✅ Firebase initialized successfully.")
except Exception as e:
    print("⚠️ Firebase initialization skipped or failed:", e)

# --- دالة إرسال الإشعارات الفورية مع النافذة المنبثقة فوق التطبيقات ---
def send_high_impact_notification(title, body, event_data=None):
    """
    إرسال إشعار فوري ذو أولوية فائقة مع حزمة بيانات تفعيل النافذة العائمة فوق التطبيقات (Overlay Window)
    """
    try:
        data_payload = {
            "click_action": "FLUTTER_NOTIFICATION_CLICK",
            "impact": "high",
            "show_overlay": "true",
            "title": str(title),
            "body": str(body)
        }
        
        if event_data:
            data_payload.update(event_data)

        message = messaging.Message(
            notification=messaging.Notification(
                title=f"🚨 خبر عاجل: {title}",
                body=body,
            ),
            data=data_payload,
            topic="high_impact_news",
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    channel_id="high_importance_channel",
                    priority="high"
                ),
            ),
        )

        response = messaging.send(message)
        print(f"[{datetime.datetime.now()}] 🔔 FCM Notification Sent: {response}")
    except Exception as e:
        print(f"❌ Error sending FCM notification: {e}")

# --- 3. إدارة وتنظيف قاعدة البيانات (حذف الأخبار الأقدم من أسبوع) ---
def cleanup_old_news_job():
    """
    وظيفة مجدولة تفحص قاعدة البيانات وتلقائياً تحذف كل خبر مر عليه 7 أيام أو أكثر.
    """
    if not db:
        return
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        one_week_ago = now_utc - datetime.timedelta(days=7)
        one_week_ago_timestamp = int(one_week_ago.timestamp() * 1000)

        docs = db.collection('articles').where('created_at_ms', '<=', one_week_ago_timestamp).stream()
        deleted_count = 0
        
        for doc in docs:
            doc.reference.delete()
            deleted_count += 1
            
        if deleted_count > 0:
            print(f"🧹 [{datetime.datetime.now()}] Auto-Cleanup: Deleted {deleted_count} news items older than 7 days.")
    except Exception as e:
        print(f"❌ Error during database cleanup: {e}")

# --- 4. إعدادات Cryptomus ---
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
    order_id = req_data.get("order_id", f"SUB_{int(time.time())}")

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

# --- 5. إعدادات جلب الأخبار وسرعة الاستجابة ---
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

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/xml, text/xml, */*'
}

def generate_doc_id(title, source):
    """توليد معرّف فريد للخبر لمنع التكرار في Firestore"""
    raw_str = f"{source}_{title}".encode('utf-8')
    return hashlib.sha256(raw_str).hexdigest()[:20]

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
        with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=3.5) as client:
            res = client.get(feed_url)
            if res.status_code == 200:
                feed = feedparser.parse(res.content)
                for entry in feed.entries[:8]:
                    title = entry.get("title", "").strip()
                    if title and is_forex_news(title):
                        image_url = ""
                        if "media_content" in entry and len(entry.media_content) > 0:
                            image_url = entry.media_content[0].get("url", "")
                        elif "enclosures" in entry and len(entry.enclosures) > 0:
                            image_url = entry.enclosures[0].get("href", "")

                        items.append({
                            "doc_id": generate_doc_id(title, source_name),
                            "title": title,
                            "link": entry.get("link", "#"),
                            "published": entry.get("published", entry.get("updated", "recent")),
                            "source": source_name,
                            "image": image_url,
                            "impact": get_impact_level(title),
                            "target_timestamp": None,
                            "created_at_ms": int(time.time() * 1000)
                        })
    except Exception:
        pass
    return items

# --- 6. دالة تحديث الأخبار والمزامنة اللحظية مع قاعدة البيانات ---
def update_news_cache_job():
    global NEWS_CACHE, NOTIFIED_NEWS_TITLES
    all_news = []
    current_time_ms = int(time.time() * 1000)

    # أ) جلب تقويم Forex Factory
    try:
        with httpx.Client(headers=HEADERS, timeout=3.0) as client:
            cal_res = client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
            if cal_res.status_code == 200:
                events = cal_res.json()
                for ev in events:
                    if ev.get("impact") in ["High", "Medium"]:
                        try:
                            dt_str = ev.get("date", "").replace("Z", "+00:00")
                            dt = datetime.datetime.fromisoformat(dt_str)
                            event_timestamp = int(dt.timestamp() * 1000)

                            if event_timestamp > current_time_ms:
                                news_title = f"{ev.get('country')} - {ev.get('title')}"
                                impact_type = "high" if ev.get("impact") == "High" else "medium"

                                item_data = {
                                    "doc_id": generate_doc_id(news_title, "Forex Factory Calendar"),
                                    "title": news_title,
                                    "link": "https://www.forexfactory.com/calendar",
                                    "published": f"موعد الصدور: {ev.get('date')}",
                                    "source": "Forex Factory Calendar",
                                    "image": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=600&q=80",
                                    "impact": impact_type,
                                    "target_timestamp": event_timestamp,
                                    "created_at_ms": current_time_ms
                                }

                                # إرسال إشعار فوري وتزامن لحظي
                                if impact_type == "high" and news_title not in NOTIFIED_NEWS_TITLES:
                                    send_high_impact_notification(
                                        title=news_title,
                                        body=f"موعد الصدور المرتقب: {ev.get('date')}",
                                        event_data={
                                            "country": str(ev.get('country', '')),
                                            "forecast": str(ev.get('forecast', '')),
                                            "previous": str(ev.get('previous', ''))
                                        }
                                    )
                                    NOTIFIED_NEWS_TITLES.add(news_title)

                                all_news.append(item_data)
                        except Exception:
                            continue
    except Exception as e:
        print("Calendar fetch error:", e)

    # ب) جلب RSS بالتوازي الفائق
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_single_feed, name, url) for name, url in RSS_FEEDS.items()]
        for future in concurrent.futures.as_completed(futures):
            feed_items = future.result()
            for item in feed_items:
                if item.get("impact") == "high" and item.get("title") not in NOTIFIED_NEWS_TITLES:
                    send_high_impact_notification(
                        title=item.get("source", "خبر عاجل"),
                        body=item.get("title", ""),
                        event_data={"link": item.get("link", "")}
                    )
                    NOTIFIED_NEWS_TITLES.add(item.get("title"))
            all_news.extend(feed_items)

    # ج) المزامنة اللحظية الحية مع Firebase Firestore
    if db and all_news:
        try:
            batch = db.batch()
            for item in all_news:
                doc_ref = db.collection('articles').document(item['doc_id'])
                batch.set(doc_ref, item, merge=True)
            batch.commit()
        except Exception as e:
            print("❌ Firestore Sync Error:", e)

    # د) تحديث الذاكرة المؤقتة لسرعة الـ API
    if all_news:
        NEWS_CACHE["data"] = all_news
        NEWS_CACHE["last_updated"] = time.time()
        print(f"⚡ [{datetime.datetime.now()}] Real-time Sync & Cache Updated! Items: {len(all_news)}")

# --- 7. تشغيل المجدول الآلي (كل 10 ثوانٍ للجلب، وكل يوم للتنظيف) ---
scheduler = BackgroundScheduler()
# فحص الأخبار والجلب الفوري كل 10 ثوانٍ
scheduler.add_job(func=update_news_cache_job, trigger="interval", seconds=10, max_instances=3)
# تنظيف قاعدة البيانات تلقائياً مرة كل 24 ساعة
scheduler.add_job(func=cleanup_old_news_job, trigger="interval", days=1, max_instances=1)
scheduler.start()

# تشغيل الجلب والتنظيف عند بداية الإقلاع
update_news_cache_job()
cleanup_old_news_job()

# --- 8. المسارات الرئيسية ---
@app.route("/", methods=["GET", "HEAD"])
def home():
    if request.method == "HEAD":
        return Response(status=200)

    news_list = []
    try:
        if db:
            articles_ref = db.collection('articles').order_by('created_at_ms', direction=firestore.Query.DESCENDING).limit(30).stream()
            news_list = [doc.to_dict() for doc in articles_ref]
    except Exception as e:
        print("Firestore Fetch Error:", e)

    try:
        return render_template('index.html', news_list=news_list)
    except Exception:
        return jsonify({"status": "live", "message": "API Server is running successfully"})

@app.route("/api/news", methods=["GET"])
def get_forex_news():
    return jsonify({
        "status": "success",
        "total_results": len(NEWS_CACHE["data"]),
        "data": NEWS_CACHE["data"],
        "cached_at": NEWS_CACHE["last_updated"]
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
