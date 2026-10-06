import hashlib
import json
import base64
import time
import datetime
import os
import threading
from collections import deque

import httpx
import feedparser
import concurrent.futures
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from deep_translator import GoogleTranslator
import firebase_admin
from firebase_admin import firestore, messaging

app = Flask(__name__)

# ✅ إصلاح: تقييد CORS لنطاقات محددة بدل فتحه للجميع
CORS(app, origins=["https://fwaadkk.onrender.com"])

# --- 1. الذاكرة المؤقتة مع قفل لحمايتها من التعارض ---
CACHE_LOCK = threading.Lock()
NEWS_CACHE = {"data": [], "last_updated": 0}

# ✅ إصلاح: مجموعة محدودة الحجم (تنظيف تلقائي) بدل نمو بلا نهاية
MAX_NOTIFIED = 5000
NOTIFIED_NEWS_TITLES = set()
NOTIFIED_QUEUE = deque()

def mark_notified(title: str) -> bool:
    """يرجع True إذا كان العنوان جديداً، ويضيفه مع تنظيف القديم"""
    if title in NOTIFIED_NEWS_TITLES:
        return False
    NOTIFIED_NEWS_TITLES.add(title)
    NOTIFIED_QUEUE.append(title)
    while len(NOTIFIED_QUEUE) > MAX_NOTIFIED:
        NOTIFIED_NEWS_TITLES.discard(NOTIFIED_QUEUE.popleft())
    return True

# ✅ إصلاح: كاش للترجمة لتجنب حظر IP وتسريع المعالجة
TRANSLATION_CACHE = {}
translator = GoogleTranslator(source='auto', target='ar')

# --- 2. تهيئة Firebase ---
db = None
try:
    firebase_admin.initialize_app()
    db = firestore.client()
    print("✅ Firebase initialized successfully.")
except Exception as e:
    print("⚠️ Firebase initialization skipped or failed:", e)

# --- 3. أدوات التحليل والترجمة ---

def translate_to_arabic(text: str) -> str:
    if not text:
        return ""
    cache_key = hashlib.md5(text.encode('utf-8')).hexdigest()
    if cache_key in TRANSLATION_CACHE:
        return TRANSLATION_CACHE[cache_key]
    try:
        translated = translator.translate(text) or text
    except Exception:
        translated = text
    # حد أقصى للكاش
    if len(TRANSLATION_CACHE) < 10000:
        TRANSLATION_CACHE[cache_key] = translated
    return translated

def extract_currencies_and_tags(title: str) -> list:
    title_upper = title.upper()
    currency_map = {
        'USD': ['USD', 'DOLLAR', 'FED', 'FOMC', 'POWELL', 'PAYROLLS', 'NFP', 'CPI'],
        'EUR': ['EUR', 'EURO', 'ECB', 'LAGARDE'],
        'GBP': ['GBP', 'POUND', 'BOE', 'STERLING'],
        'JPY': ['JPY', 'YEN', 'BOJ'],
        'GOLD': ['GOLD', 'XAU', 'BULLION'],
        'BTC': ['BTC', 'BITCOIN', 'CRYPTO'],
        'AUD': ['AUD', 'AUSSIE'],
        'CAD': ['CAD', 'LOONIE'],
        'CHF': ['CHF', 'FRANC']
    }
    tags = set()
    for curr, keywords in currency_map.items():
        if any(k in title_upper for k in keywords):
            tags.add(curr)
    return list(tags) if tags else ['GENERAL']

def analyze_market_sentiment(title: str) -> str:
    title_lower = title.lower()
    bullish_words = ['surge', 'jump', 'rise', 'growth', 'gain', 'bullish', 'hike', 'beat', 'strong', 'positive']
    bearish_words = ['drop', 'fall', 'plunge', 'decline', 'loss', 'bearish', 'cut', 'miss', 'weak', 'negative', 'slump']
    has_bullish = any(w in title_lower for w in bullish_words)
    has_bearish = any(w in title_lower for w in bearish_words)
    if has_bullish and not has_bearish:
        return "bullish"
    elif has_bearish and not has_bullish:
        return "bearish"
    return "neutral"

# --- 4. إشعارات FCM ---
def send_high_impact_notification(title, title_ar, body, event_data=None, is_pro_only=False):
    try:
        topic_target = "pro_users_news" if is_pro_only else "high_impact_news"
        data_payload = {
            "click_action": "FLUTTER_NOTIFICATION_CLICK",
            "impact": "high",
            "show_overlay": "true",
            "title": str(title_ar or title),
            "body": str(body),
            "is_pro": "true" if is_pro_only else "false"
        }
        if event_data:
            data_payload.update(event_data)

        message = messaging.Message(
            notification=messaging.Notification(
                title=f"🚨 {title_ar or title}",
                body=body,
            ),
            data=data_payload,
            topic=topic_target,
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
        print(f"[{datetime.datetime.now()}] 🔔 FCM Sent ({topic_target}): {response}")
    except Exception as e:
        print(f"❌ FCM Notification Error: {e}")

# --- 5. تنظيف قاعدة البيانات ---
def cleanup_old_news_job():
    if not db:
        return
    try:
        one_week_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
        ts = int(one_week_ago.timestamp() * 1000)
        docs = db.collection('articles').where('created_at_ms', '<=', ts).stream()
        deleted = 0
        for doc in docs:
            doc.reference.delete()
            deleted += 1
        if deleted:
            print(f"🧹 Auto-Cleanup: Deleted {deleted} old news items.")
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")

# --- 6. Cryptomus والدفع ---

# ✅ إصلاح: كل الأسرار من متغيرات البيئة
CRYPTOMUS_MERCHANT_ID = os.environ.get("CRYPTOMUS_MERCHANT_ID", "")
CRYPTOMUS_PAYMENT_KEY = os.environ.get("CRYPTOMUS_PAYMENT_KEY", "")

# ✅ إصلاح: السعر ثابت في السيرفر — لا يقبل أي مبلغ من العميل
SUBSCRIPTION_PRICE_USD = "10.00"
BASE_URL = "https://fwaadkk.onrender.com"

@app.route("/cryptomus_b9c7b8dd.html", methods=["GET"])
def cryptomus_verification():
    return Response("cryptomus=b9c7b8dd", mimetype="text/plain")

def generate_cryptomus_signature(data_dict: dict, api_key: str) -> str:
    json_data = json.dumps(data_dict, separators=(',', ':')).encode('utf-8')
    base64_data = base64.b64encode(json_data).decode('utf-8')
    return hashlib.md5((base64_data + api_key).encode('utf-8')).hexdigest()

@app.route("/api/create-invoice", methods=["POST"])
def create_subscription_invoice():
    if not CRYPTOMUS_PAYMENT_KEY or not CRYPTOMUS_MERCHANT_ID:
        return jsonify({"status": "error", "message": "Payment gateway not configured"}), 500

    req_data = request.get_json(silent=True) or {}
    user_id = str(req_data.get("user_id", "")).strip()
    # ✅ إصلاح: رفض الطلب بدون user_id صالح
    if not user_id or user_id == "guest":
        return jsonify({"status": "error", "message": "Valid user_id required"}), 400

    # ✅ إصلاح: استخدام "." كفاصل لأن "_" قد يكون جزءاً من user_id
    order_id = f"SUB.{user_id}.{int(time.time())}"

    payload = {
        "amount": SUBSCRIPTION_PRICE_USD,  # ✅ سعر ثابت من السيرفر
        "currency": "USD",
        "order_id": order_id,
        "network": "TRON",
        "to_currency": "USDT",
        "url_callback": f"{BASE_URL}/api/cryptomus-webhook",
        "url_success": f"{BASE_URL}/?payment=success",
        "additional_data": user_id,  # ✅ نسخة احتياطية موثوقة للـ user_id
        "is_payment_multiple": False,
        "lifetime": 3600
    }

    headers = {
        "merchant": CRYPTOMUS_MERCHANT_ID,
        "sign": generate_cryptomus_signature(payload, CRYPTOMUS_PAYMENT_KEY),
        "Content-Type": "application/json"
    }

    try:
        with httpx.Client() as client:
            response = client.post(
                "https://api.cryptomus.com/v1/payment",
                json=payload, headers=headers, timeout=15.0
            )
        res_data = response.json()
        if response.status_code == 200 and res_data.get("state") == 0:
            return jsonify({
                "status": "success",
                "payment_url": res_data["result"]["url"],
                "invoice_id": res_data["result"]["uuid"]
            })
        return jsonify({"status": "error", "message": res_data.get("message", "Failed")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def verify_webhook_signature(data: dict, received_sign: str) -> bool:
    """✅ إصلاح حرج: التحقق من أن الـ webhook قادم فعلاً من Cryptomus"""
    if not received_sign or not CRYPTOMUS_PAYMENT_KEY:
        return False
    expected = generate_cryptomus_signature(data, CRYPTOMUS_PAYMENT_KEY)
    # مقارنة آمنة ضد timing attacks
    return hashlib.compare_digest(expected, received_sign) if hasattr(hashlib, 'compare_digest') else expected == received_sign

@app.route("/api/cryptomus-webhook", methods=["POST"])
def cryptomus_webhook():
    try:
        data = request.get_json(silent=True) or {}
        received_sign = request.headers.get("sign", "")

        # ✅ إصلاح حرج: رفض أي طلب بدون توقيع صحيح
        if not verify_webhook_signature(data, received_sign):
            print("⚠️ Webhook with invalid signature rejected!")
            return jsonify({"error": "invalid signature"}), 403

        status = data.get("status")
        order_id = str(data.get("order_id", ""))

        if status in ["paid", "paid_over"] and db and order_id.startswith("SUB."):
            # ✅ إصلاح: استخراج user_id بشكل صحيح مع فاصل "."
            parts = order_id.split(".")
            user_id = parts[1] if len(parts) >= 3 else data.get("additional_data", "")
            if user_id:
                db.collection("users").document(user_id).set({
                    "is_pro": True,
                    "pro_since": int(time.time() * 1000),
                    "subscription_status": "active"
                }, merge=True)
                print(f"✅ User {user_id} upgraded to PRO successfully.")
            return jsonify({"status": "ok"})

        return jsonify({"status": "ignored"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# --- 7. مصادر الأخبار ---
RSS_FEEDS = {
    "ForexLive": "https://www.forexlive.com/feed/news",
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "DailyFX": "https://www.dailyfx.com/feeds/market-news",
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "Investing.com Central Banks": "https://www.investing.com/rss/news_14.rss",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Yahoo Finance Currency": "https://finance.yahoo.com/news/rssindex"
}

HIGH_IMPACT = ['fed', 'interest rate', 'cpi', 'nfp', 'powell', 'inflation', 'ecb', 'central bank', 'fomc', 'gdp']
MEDIUM_IMPACT = ['dollar', 'euro', 'pound', 'yen', 'usd', 'eur', 'gbp', 'jpy', 'trade', 'retail', 'jobless', 'treasury', 'yield', 'gold', 'xau']

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/xml, text/xml, */*'
}

def generate_doc_id(title, source):
    return hashlib.sha256(f"{source}_{title}".encode('utf-8')).hexdigest()[:20]

def get_impact_level(title):
    t = title.lower()
    if any(k in t for k in HIGH_IMPACT):
        return "high"
    if any(k in t for k in MEDIUM_IMPACT):
        return "medium"
    return "low"

def is_forex_news(title):
    t = title.lower()
    return any(k in t for k in (HIGH_IMPACT + MEDIUM_IMPACT + ['forex', 'fx', 'currency', 'gold', 'btc']))

def fetch_single_feed(source_name, feed_url):
    items = []
    try:
        with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=10.0) as client:
            res = client.get(feed_url)
            if res.status_code != 200:
                return items
            feed = feedparser.parse(res.content)
            # ✅ إصلاح: التحقق من صلاحية الـ feed (تجاهل صفحات الخطأ HTML)
            if feed.bozo and not feed.entries:
                return items
            for entry in feed.entries[:8]:
                title = entry.get("title", "").strip()
                if not title or not is_forex_news(title):
                    continue
                image_url = ""
                if entry.get("media_content"):
                    image_url = entry.media_content[0].get("url", "")
                elif entry.get("enclosures"):
                    image_url = entry.enclosures[0].get("href", "")

                items.append({
                    "doc_id": generate_doc_id(title, source_name),
                    "title": title,
                    "title_ar": translate_to_arabic(title),
                    "link": entry.get("link", "#"),
                    "published": entry.get("published", entry.get("updated", "recent")),
                    "source": source_name,
                    "image": image_url,
                    "impact": get_impact_level(title),
                    "tags": extract_currencies_and_tags(title),
                    "sentiment": analyze_market_sentiment(title),
                    "target_timestamp": None,
                    "created_at_ms": int(time.time() * 1000)
                })
    except Exception:
        pass
    return items

# ✅ إصلاح: تقسيم الـ batch لأن حد Firestore هو 500 عملية
def save_batch_chunked(items):
    CHUNK = 450
    for i in range(0, len(items), CHUNK):
        batch = db.batch()
        for item in items[i:i + CHUNK]:
            doc_ref = db.collection('articles').document(item['doc_id'])
            batch.set(doc_ref, item, merge=True)
        batch.commit()

# --- 8. مهمة التحديث الرئيسية ---
def update_news_cache_job():
    all_news = []
    current_time_ms = int(time.time() * 1000)

    # أ) تقويم Forex Factory
    try:
        with httpx.Client(headers=HEADERS, timeout=10.0) as client:
            cal_res = client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
            if cal_res.status_code == 200:
                for ev in cal_res.json():
                    if ev.get("impact") not in ["High", "Medium"]:
                        continue
                    try:
                        dt = datetime.datetime.fromisoformat(ev.get("date", "").replace("Z", "+00:00"))
                        event_ts = int(dt.timestamp() * 1000)
                        if event_ts <= current_time_ms:
                            continue
                        news_title = f"{ev.get('country')} - {ev.get('title')}"
                        title_ar = f"{ev.get('country')} - {translate_to_arabic(ev.get('title'))}"
                        impact_type = "high" if ev.get("impact") == "High" else "medium"

                        all_news.append({
                            "doc_id": generate_doc_id(news_title, "Forex Factory Calendar"),
                            "title": news_title,
                            "title_ar": title_ar,
                            "link": "https://www.forexfactory.com/calendar",
                            "published": f"موعد الصدور: {ev.get('date')}",
                            "source": "Forex Factory Calendar",
                            "image": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=600&q=80",
                            "impact": impact_type,
                            "tags": [ev.get('country', 'USD')],
                            "sentiment": "neutral",
                            "target_timestamp": event_ts,
                            "created_at_ms": current_time_ms
                        })

                        if impact_type == "high" and mark_notified(news_title):
                            send_high_impact_notification(
                                title=news_title, title_ar=title_ar,
                                body=f"موعد الصدور المرتقب: {ev.get('date')}",
                                event_data={
                                    "country": str(ev.get('country', '')),
                                    "forecast": str(ev.get('forecast', '')),
                                    "previous": str(ev.get('previous', ''))
                                },
                                is_pro_only=False
                            )
                    except Exception:
                        continue
    except Exception as e:
        print("Calendar fetch error:", e)

    # ب) جلب RSS بالتوازي
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_single_feed, n, u) for n, u in RSS_FEEDS.items()]
        for future in concurrent.futures.as_completed(futures):
            for item in future.result():
                if mark_notified(item["title"]):
                    send_high_impact_notification(
                        title=item["title"],
                        title_ar=item["title_ar"],
                        body=f"المصدر: {item['source']}",
                        event_data={"link": item.get("link", "")},
                        is_pro_only=(item["impact"] != "high")
                    )
                all_news.append(item)

    # ج) الحفظ في Firebase
    if db and all_news:
        try:
            save_batch_chunked(all_news)
        except Exception as e:
            print("❌ Firestore Sync Error:", e)

    # ✅ إصلاح: تحديث الكاش فقط عند وجود بيانات جديدة (مع قفل)
    if all_news:
        with CACHE_LOCK:
            NEWS_CACHE["data"] = all_news
            NEWS_CACHE["last_updated"] = time.time()
        print(f"⚡ [{datetime.datetime.now()}] Engine Refreshed: {len(all_news)} articles processed.")

# --- 9. المجدول ---
# ✅ إصلاح: كل 60 ثانية بدل 10، ومهمة واحدة فقط في نفس الوقت
scheduler = BackgroundScheduler()
scheduler.add_job(func=update_news_cache_job, trigger="interval", seconds=60,
                  max_instances=1, coalesce=True)
scheduler.add_job(func=cleanup_old_news_job, trigger="interval", days=1, max_instances=1)
scheduler.start()

update_news_cache_job()
cleanup_old_news_job()

# --- 10. API Endpoints ---
@app.route("/", methods=["GET", "HEAD"])
def home():
    if request.method == "HEAD":
        return Response(status=200)
    news_list = []
    try:
        if db:
            ref = db.collection('articles').order_by(
                'created_at_ms', direction=firestore.Query.DESCENDING).limit(30).stream()
            news_list = [doc.to_dict() for doc in ref]
    except Exception as e:
        print("Firestore Fetch Error:", e)
    try:
        return render_template('index.html', news_list=news_list)
    except Exception:
        return jsonify({"status": "live", "message": "Pro Forex API Engine is running."})

@app.route("/api/news", methods=["GET"])
def get_forex_news():
    tag_filter = request.args.get('tag', '').upper()
    with CACHE_LOCK:
        data = list(NEWS_CACHE["data"])
        cached_at = NEWS_CACHE["last_updated"]
    if tag_filter:
        data = [item for item in data if tag_filter in item.get('tags', [])]
    return jsonify({
        "status": "success",
        "total_results": len(data),
        "data": data,
        "cached_at": cached_at
    })

@app.route("/api/user-status", methods=["GET"])
def check_user_status():
    user_id = request.args.get('user_id')
    if not user_id or not db:
        return jsonify({"is_pro": False})
    try:
        doc = db.collection("users").document(user_id).get()
        if doc.exists:
            return jsonify({"is_pro": doc.to_dict().get("is_pro", False)})
    except Exception:
        pass
    return jsonify({"is_pro": False})

if __name__ == '__main__':
    # ✅ إصلاح: debug معطل — للإنتاج استخدم: gunicorn -w 2 -b 0.0.0.0:8080 app:app
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
