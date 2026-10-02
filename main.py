import hashlib
import json
import base64
import time
import datetime
import httpx
import feedparser
from flask import Flask, render_template, request, jsonify, Response, redirect
import firebase_admin
from firebase_admin import firestore

app = Flask(__name__)

# تهيئة قاعدة بيانات Firebase Firestore تلقائياً عبر بيئة Google Cloud Shell / Render
try:
    firebase_admin.initialize_app()
except ValueError:
    pass  # تفادي خطأ التكرار لو تم تهيئتها مسبقاً

db = firestore.client()

# إعدادات بوابة الدفع Cryptomus
CRYPTOMUS_MERCHANT_ID = "b9c7b8dd-cc24-4c13-beef-1f97ae33f932"
CRYPTOMUS_PAYMENT_KEY = "YOUR_PAYMENT_API_KEY_HERE"

# 1. مسار التحقق الخاص بـ Cryptomus (مطلوب لقبول الموقع)
@app.route("/cryptomus_b9c7b8dd.html", methods=["GET"])
def cryptomus_verification():
    return Response("cryptomus=b9c7b8dd", mimetype="text/plain")

# دالة توقيع Cryptomus
def generate_cryptomus_signature(data_dict: dict, api_key: str) -> str:
    json_data = json.dumps(data_dict, separators=(',', ':')).encode('utf-8')
    base64_data = base64.b64encode(json_data).decode('utf-8')
    sign_str = base64_data + api_key
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

# 2. مسار إنشاء فاتورة الدفع
@app.route("/api/create-invoice", methods=["POST"])
def create_subscription_invoice():
    if CRYPTOMUS_PAYMENT_KEY == "YOUR_PAYMENT_API_KEY_HERE":
        return jsonify({"status": "error", "message": "Key missing"}), 400

    # استلام البيانات أو استخدام قيم افتراضية
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
        # استخدام httpx بشكل متزامن داخل Flask
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
            return jsonify({
                "status": "error",
                "message": res_data.get("message", "Failed")
            })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# 3. مسار استقبال الويب هوك (Webhook) من Cryptomus
@app.route("/api/cryptomus-webhook", methods=["POST"])
def cryptomus_webhook():
    try:
        data = request.json or {}
        status = data.get("status")

        if status in ["paid", "paid_over"]:
            # هنا يمكنك تحديث حالة الدفع في قاعدة بيانات Firestore إذا رغبت
            return jsonify({"status": "ok"})

        return jsonify({"status": "ignored"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# إعدادات مصادر الأخبار ومستويات الأهمية
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

# 4. الصفحة الرئيسية (تربط Flask بقاعدة بيانات Firestore وعرض القالب الاحترافي)
@app.route("/")
def home():
    try:
        # جلب البيانات المخزنة مسبقاً في مجموعة 'articles' بقاعدة بيانات Firestore
        articles_ref = db.collection('articles').stream()
        news_list = [doc.to_dict() for doc in articles_ref]
        
        # تمرير الأخبار لعرضها في ملف index.html
        return render_template('index.html', news_list=news_list)
    except Exception as e:
        return f"حدث خطأ في الاتصال بقاعدة البيانات أو عرض الصفحة: {str(e)}"

# 5. مسار جلب الأخبار الحية وجدول الفوركس (API)
@app.route("/api/news", methods=["GET"])
def get_forex_news():
    all_news = []
    current_time_ms = int(time.time() * 1000)
    
    try:
        with httpx.Client() as client:
            cal_res = client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=5.0)
            if cal_res.status_code == 200:
                events = cal_res.json()
                for ev in events:
                    if ev.get("impact") in ["High", "Medium"]:
                        dt = datetime.datetime.fromisoformat(ev.get("date").replace("Z", "+00:00"))
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
    except Exception as e:
        print("Calendar fetch error:", e)

    for source_name, feed_url in RSS_FEEDS.items():
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

                    all_news.append({
                        "title": title,
                        "link": entry.get("link", "#"),
                        "published": entry.get("published", entry.get("updated", "recent")),
                        "source": source_name,
                        "image": image_url,
                        "impact": get_impact_level(title),
                        "target_timestamp": None
                    })
        except Exception:
            continue

    return jsonify({
        "status": "success",
        "total_results": len(all_news),
        "data": all_news
    })

# 6. مسار إضافي لتجربة إضافة خبر لقاعدة البيانات وإثبات نشاط الموقع
@app.route('/add-sample')
def add_sample():
    try:
        db.collection('articles').add({
            'title': 'تحديثات أسواق العملات والتحليل الفني',
            'content': 'تستمر الأسواق في تفاعل البيانات الاقتصادية الصادرة حديثاً مع ترقب لتوجيهات البنوك الكبرى.'
        })
        return "تم إضافة خبر تجريبي بنجاح لقاعدة البيانات! <a href='/'>العودة للصفحة الرئيسية</a>"
    except Exception as e:
        return f"حدث خطأ أثناء الإضافة: {str(e)}"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
