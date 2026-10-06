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
from deep_translator import GoogleTranslator
import firebase_admin
from firebase_admin import firestore, messaging

app = Flask(__name__)
CORS(app)

# --- 1. تهيئة الذاكرة المؤقتة (Cache) ---
NEWS_CACHE = {
    "data": [],
    "last_updated": 0
}

NOTIFIED_NEWS_TITLES = set()

# --- 2. تهيئة Firebase بشكل آمن ومنع خطأ التطبيق الافتراضي ---
db = None
try:
    firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    
    if firebase_creds_json:
        try:
            creds_dict = json.loads(firebase_creds_json)
        except json.JSONDecodeError:
            fixed_json = firebase_creds_json.replace("\\n", "\n")
            creds_dict = json.loads(fixed_json)
            
        cred = firebase_admin.credentials.Certificate(creds_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        print("✅ Firebase initialized successfully from Environment Variables.")
    else:
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        print("⚠️ Firebase initialized using default method.")
        
    db = firestore.client()
except Exception as e:
    print("⚠️ Firebase initialization failed:", e)

# --- 3. أدوات التحليل والترجمة الذكية ---

def translate_to_arabic(text: str) -> str:
    """ترجمة العنوان تلقائياً إلى العربية مع معالجة الأخطاء السريعة"""
    if not text:
        return ""
    try:
        translated = GoogleTranslator(source='auto', target='ar').translate(text)
        return translated if translated else text
    except Exception:
        return text

def extract_currencies_and_tags(title: str) -> list:
    """استخراج العملات والأزواج المتأثرة من عنوان الخبر"""
    title_upper = title.upper()
    currency_map = {
        'USD': ['USD', 'DOLLAR', 'FED', 'FOMC', 'POWELL', 'PAYROLLS', 'NFP', 'CPI'],
        'EUR': ['EUR', 'EURO', 'ECB', 'LAGARDE'],
        'GBP': ['GBP', 'POUND', 'BOE', 'STERLING'],
        'JPY': ['JPY', 'YEN', 'BOJ'],
        'GOLD': ['GOLD', 'XAU', 'BULLION'],
        'BTC': ['BTC', 'BITCOIN', 'CRYPTO'],
        'AUD': ['AUD', 'Aussie'],
        'CAD': ['CAD', 'Loonie'],
        'CHF': ['CHF', 'FRANC']
    }
    
    tags = set()
    for curr, keywords in currency_map.items():
        if any(k in title_upper for k in keywords):
            tags.add(curr)
            
    return list(tags) if tags else ['GENERAL']

def analyze_market_sentiment(title: str) -> str:
    """تحليل الانطباع الأولي للخبر (إيجابي / سلبي / حيادي)"""
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

# --- 4. دالة إرسال الإشعارات الفورية المزودة بتقسيم Pro / Free ---
def send_high_impact_notification(title, title_ar, body, event_data=None, is_pro_only=False):
    """
    إرسال إشعار فوري FCM مع توجيهه حسب نوع اشتراك المستخدم
    """
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

# --- 5. إدارة وتنظيف قاعدة البيانات ---
def cleanup_old_news_job():
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
            print(f"🧹 Auto-Cleanup: Deleted {deleted_count} old news items.")
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")

# --- 6. إعدادات Cryptomus والدفع ---
CRYPTOMUS_MERCHANT_ID = "23a84c54-0c08-4feb-b5fc-bcc05a3a218f"
CRYPTOMUS_PAYMENT_KEY = os.environ.get("CRYPTOMUS_PAYMENT_KEY", "YOUR_PAYMENT_API_KEY_HERE")

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
        return jsonify({"status": "error", "message": "Cryptomus Payment Key missing"}), 400

    req_data = request.json or {}
    user_id = req_data.get("user_id", "guest")
    amount = req_data.get("amount", "10.00")
    order_id = req_data.get("order_id", f"SUB_{user_id}_{int(time.time())}")

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
        order_id = data.get("order_id", "")

        if status in ["paid", "paid_over"]:
            if db and order_id.startswith("SUB_"):
                parts = order_id.split("_")
                if len(parts) >= 2:
                    user_id = parts[1]
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

# --- 7. إعدادات الخلاصات ومصادر الأخبار المحدثة ---
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
    return any(k in title_lower for k in (HIGH_IMPACT + MEDIUM_IMPACT + ['forex', 'fx', 'currency', 'gold', 'btc']))

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

                        title_ar = translate_to_arabic(title)
                        tags = extract_currencies_and_tags(title)
                        sentiment = analyze_market_sentiment(title)

                        items.append({
                            "doc_id": generate_doc_id(title, source_name),
                            "title": title,
                            "title_ar": title_ar,
                            "link": entry.get("link", "#"),
                            "published": entry.get("published", entry.get("updated", "recent")),
                            "source": source_name,
                            "image": image_url,
                            "impact": get_impact_level(title),
                            "tags": tags,
                            "sentiment": sentiment,
                            "target_timestamp": None,
                            "created_at_ms": int(time.time() * 1000)
                        })
    except Exception:
        pass
    return items

# --- 8. تحديث الذاكرة والمزامنة اللحظية مع Firebase ---
def update_news_cache_job():
    global NEWS_CACHE, NOTIFIED_NEWS_TITLES
    all_news = []
    current_time_ms = int(time.time() * 1000)

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
                                title_ar = f"{ev.get('country')} - {translate_to_arabic(ev.get('title'))}"
                                impact_type = "high" if ev.get("impact") == "High" else "medium"

                                item_data = {
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
                                    "target_timestamp": event_timestamp,
                                    "created_at_ms": current_time_ms
                                }

                                if impact_type == "high" and news_title not in NOTIFIED_NEWS_TITLES:
                                    send_high_impact_notification(
                                        title=news_title,
                                        title_ar=title_ar,
                                        body=f"موعد الصدور المرتقب: {ev.get('date')}",
                                        event_data={
                                            "country": str(ev.get('country', '')),
                                            "forecast": str(ev.get('forecast', '')),
                                            "previous": str(ev.get('previous', ''))
                                        },
                                        is_pro_only=False
                                    )
                                    NOTIFIED_NEWS_TITLES.add(news_title)

                                all_news.append(item_data)
                        except Exception:
                            continue
    except Exception as e:
        print("Calendar fetch error:", e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_single_feed, name, url) for name, url in RSS_FEEDS.items()]
        for future in concurrent.futures.as_completed(futures):
            feed_items = future.result()
            for item in feed_items:
                if item.get("title") not in NOTIFIED_NEWS_TITLES:
                    is_high = item.get("impact") == "high"
                    send_high_impact_notification(
                        title=item.get("title"),
                        title_ar=item.get("title_ar"),
                        body=f"المصدر: {item.get('source')}",
                        event_data={"link": item.get("link", "")},
                        is_pro_only=not is_high
                    )
                    NOTIFIED_NEWS_TITLES.add(item.get("title"))
            all_news.extend(feed_items)

    if db and all_news:
        try:
            batch = db.batch()
            for item in all_news:
                doc_ref = db.collection('articles').document(item['doc_id'])
                batch.set(doc_ref, item, merge=True)
            batch.commit()
        except Exception as e:
            print("❌ Firestore Sync Error:", e)

    if all_news:
        NEWS_CACHE["data"] = all_news
        NEWS_CACHE["last_updated"] = time.time()
        print(f"⚡ [{datetime.datetime.now()}] Engine Refreshed: {len(all_news)} articles processed.")

# --- 9. المجدول الآلي ---
scheduler = BackgroundScheduler()
scheduler.add_job(func=update_news_cache_job, trigger="interval", seconds=10, max_instances=3)
scheduler.add_job(func=cleanup_old_news_job, trigger="interval", days=1, max_instances=1)
scheduler.start()

update_news_cache_job()
cleanup_old_news_job()

# --- 10. API Endpoints المحدثة ---
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
        return jsonify({"status": "live", "message": "Pro Forex API Engine is running."})

@app.route("/api/news", methods=["GET"])
def get_forex_news():
    tag_filter = request.args.get('tag', '').upper()
    data = NEWS_CACHE["data"]
    
    if tag_filter:
        data = [item for item in data if tag_filter in item.get('tags', [])]
        
    return jsonify({
        "status": "success",
        "total_results": len(data),
        "data": data,
        "cached_at": NEWS_CACHE["last_updated"]
    })

@app.route("/api/user-status", methods=["GET"])
def check_user_status():
    user_id = request.args.get('user_id')
    if not user_id or not db:
        return jsonify({"is_pro": False})
    
    try:
        user_doc = db.collection("users").document(user_id).get()
        if user_doc.exists:
            return jsonify({"is_pro": user_doc.to_dict().get("is_pro", False)})
    except Exception:
        pass
    
    return jsonify({"is_pro": False})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
