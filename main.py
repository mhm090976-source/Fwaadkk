import base64
import concurrent.futures
import datetime
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import deque

import feedparser
import firebase_admin
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from firebase_admin import auth as fb_auth
from firebase_admin import credentials, firestore, messaging
from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://fwaadkk.onrender.com"])

# ============================================================
# الإعدادات (متغيرات البيئة)
# ============================================================
CRYPTOMUS_MERCHANT_ID = os.environ.get("CRYPTOMUS_MERCHANT_ID", "")
CRYPTOMUS_PAYMENT_KEY = os.environ.get("CRYPTOMUS_PAYMENT_KEY", "")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")

REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "0") == "1"

SUBSCRIPTION_PRICE_USD = "10.00"
SUBSCRIPTION_DAYS = 30
BASE_URL = "https://fwaadkk.onrender.com"

# ============================================================
# 1. Firebase
# ============================================================
db = None
FIREBASE_READY = False
try:
    if FIREBASE_CREDENTIALS:
        cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
        firebase_admin.initialize_app(cred)
    else:
        firebase_admin.initialize_app()
    db = firestore.client()
    FIREBASE_READY = True
    print("✅ Firebase initialized successfully.")
except Exception as e:
    print("⚠️ Firebase initialization failed:", e)

# ============================================================
# 2. إدارة معرفات الأخبار (منع التكرار)
# ============================================================
MAX_KNOWN = 2000  # تقليل الحجم لتوفير الذاكرة
KNOWN_IDS = set()
KNOWN_QUEUE = deque()
_preloaded = False
_first_cycle = True
_suppress_first_notify = False


def is_known(doc_id: str) -> bool:
    return doc_id in KNOWN_IDS


def remember(doc_id: str):
    if doc_id in KNOWN_IDS:
        return
    KNOWN_IDS.add(doc_id)
    KNOWN_QUEUE.append(doc_id)
    while len(KNOWN_QUEUE) > MAX_KNOWN:
        KNOWN_IDS.discard(KNOWN_QUEUE.popleft())


def ensure_preloaded():
    global _preloaded, _suppress_first_notify
    if _preloaded:
        return
    _preloaded = True
    if not db:
        _suppress_first_notify = True
        return
    try:
        docs = (
            db.collection("articles")
            .order_by("created_at_ms", direction=firestore.Query.DESCENDING)
            .limit(500)  # جلب عدد أقل لتوفير استهلاك الذاكرة عند البدء
            .select([])
            .stream()
        )
        for d in docs:
            remember(d.id)
    except Exception as e:
        print("⚠️ Preload known ids failed:", e)
    _suppress_first_notify = len(KNOWN_IDS) == 0


# ============================================================
# 3. التحليل
# ============================================================
def build_pattern(words, suffix=False):
    parts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    tail = r"(?:s|es|d|ed|ing)?" if suffix else ""
    return re.compile(rf"\b(?:{parts}){tail}\b", re.IGNORECASE)


CURRENCY_PATTERNS = {
    "USD": build_pattern(["USD", "DOLLAR", "FED", "FEDERAL RESERVE", "FOMC", "POWELL", "PAYROLLS", "NFP", "CPI"]),
    "EUR": build_pattern(["EUR", "EURO", "ECB", "LAGARDE"]),
    "GBP": build_pattern(["GBP", "POUND", "BOE", "STERLING"]),
    "JPY": build_pattern(["JPY", "YEN", "BOJ"]),
    "GOLD": build_pattern(["GOLD", "XAU", "BULLION"]),
    "BTC": build_pattern(["BTC", "BITCOIN", "CRYPTO"]),
    "AUD": build_pattern(["AUD", "AUSSIE"]),
    "CAD": build_pattern(["CAD", "LOONIE"]),
    "CHF": build_pattern(["CHF", "FRANC"]),
}

BULLISH_RE = build_pattern(
    ["surge", "jump", "rise", "rising", "growth", "gain", "bullish", "hike", "beat", "strong", "positive", "rally"],
    suffix=True,
)
BEARISH_RE = build_pattern(
    ["drop", "fall", "falling", "plunge", "decline", "loss", "bearish", "cut", "miss", "weak", "negative", "slump"],
    suffix=True,
)


def extract_currencies_and_tags(title: str) -> list:
    tags = [cur for cur, pat in CURRENCY_PATTERNS.items() if pat.search(title)]
    return tags if tags else ["GENERAL"]


def analyze_market_sentiment(title: str) -> str:
    bull = bool(BULLISH_RE.search(title))
    bear = bool(BEARISH_RE.search(title))
    if bull and not bear:
        return "bullish"
    if bear and not bull:
        return "bearish"
    return "neutral"


# ============================================================
# 4. إشعارات FCM
# ============================================================
def send_high_impact_notification(title, body, event_data=None, is_pro_only=False):
    if not FIREBASE_READY:
        return
    try:
        topic_target = "pro_users_news" if is_pro_only else "high_impact_news"
        data_payload = {
            "click_action": "FLUTTER_NOTIFICATION_CLICK",
            "impact": "high",
            "show_overlay": "true",
            "title": str(title),
            "body": str(body),
            "is_pro": "true" if is_pro_only else "false",
        }
        if event_data:
            data_payload.update({k: str(v) for k, v in event_data.items()})

        message = messaging.Message(
            notification=messaging.Notification(title=f"🚨 {title}", body=body),
            data=data_payload,
            topic=topic_target,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    channel_id="high_importance_channel",
                    priority="high",
                ),
            ),
        )
        response = messaging.send(message)
    except Exception as e:
        print(f"❌ FCM Notification Error: {e}")


# ============================================================
# 5. تنظيف قاعدة البيانات
# ============================================================
def cleanup_old_news_job():
    if not db:
        return
    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)  # تقليص الحفظ لـ 3 أيام لتخفيف حجم قاعدة البيانات
        ts = int(cutoff.timestamp() * 1000)
        deleted = 0
        while True:
            docs = list(
                db.collection("articles").where("created_at_ms", "<=", ts).limit(200).stream()
            )
            if not docs:
                break
            batch = db.batch()
            for d in docs:
                batch.delete(d.reference)
            batch.commit()
            deleted += len(docs)
        if deleted:
            print(f"🧹 Auto-Cleanup: Deleted {deleted} old news items.")
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")


# ============================================================
# 6. Cryptomus والدفع
# ============================================================
@app.route("/cryptomus_b9c7b8dd.html", methods=["GET"])
def cryptomus_verification():
    return Response("cryptomus=b9c7b8dd", mimetype="text/plain")


def cryptomus_sign(raw_body: str, api_key: str) -> str:
    b64 = base64.b64encode(raw_body.encode("utf-8")).decode("utf-8")
    return hashlib.md5((b64 + api_key).encode("utf-8")).hexdigest()


def verify_webhook(data: dict, header_sign: str) -> bool:
    if not CRYPTOMUS_PAYMENT_KEY:
        return False
    data = dict(data)
    received = data.pop("sign", None) or header_sign
    if not received:
        return False
    raw = json.dumps(data, separators=(",", ":")).replace("/", "\\/")
    expected = cryptomus_sign(raw, CRYPTOMUS_PAYMENT_KEY)
    return hmac.compare_digest(expected, str(received))


def get_request_uid(claimed_uid: str):
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        if not FIREBASE_READY:
            return None
        try:
            return fb_auth.verify_id_token(header[7:])["uid"]
        except Exception:
            return None
    if REQUIRE_AUTH:
        return None
    return claimed_uid or None


_RATE = {}
_RATE_LOCK = threading.Lock()


def rate_limited(key: str, limit: int = 5, window: int = 60) -> bool:
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _RATE[key] = hits
            return True
        hits.append(now)
        _RATE[key] = hits
        if len(_RATE) > 1000:
            for k in [k for k, v in _RATE.items() if not v or now - v[-1] > window]:
                _RATE.pop(k, None)
    return False


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


@app.route("/api/create-invoice", methods=["POST"])
def create_subscription_invoice():
    if not CRYPTOMUS_PAYMENT_KEY or not CRYPTOMUS_MERCHANT_ID:
        return jsonify({"status": "error", "message": "Payment gateway not configured"}), 500

    if rate_limited(f"invoice:{client_ip()}"):
        return jsonify({"status": "error", "message": "Too many requests"}), 429

    req_data = request.get_json(silent=True) or {}
    claimed = str(req_data.get("user_id", "")).strip()
    user_id = get_request_uid(claimed)
    if not user_id or user_id == "guest":
        return jsonify({"status": "error", "message": "Valid user required"}), 401

    order_id = f"SUB.{user_id}.{int(time.time())}"
    payload = {
        "amount": SUBSCRIPTION_PRICE_USD,
        "currency": "USD",
        "order_id": order_id,
        "network": "TRON",
        "to_currency": "USDT",
        "url_callback": f"{BASE_URL}/api/cryptomus-webhook",
        "url_success": f"{BASE_URL}/?payment=success",
        "additional_data": user_id,
        "is_payment_multiple": False,
        "lifetime": 3600,
    }

    raw_body = json.dumps(payload, separators=(",", ":"))
    headers = {
        "merchant": CRYPTOMUS_MERCHANT_ID,
        "sign": cryptomus_sign(raw_body, CRYPTOMUS_PAYMENT_KEY),
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client() as client:
            response = client.post(
                "https://api.cryptomus.com/v1/payment",
                content=raw_body.encode("utf-8"),
                headers=headers,
                timeout=15.0,
            )
        res_data = response.json()
        if response.status_code == 200 and res_data.get("state") == 0:
            return jsonify({
                "status": "success",
                "payment_url": res_data["result"]["url"],
                "invoice_id": res_data["result"]["uuid"],
            })
        return jsonify({"status": "error", "message": "Failed to create invoice"}), 502
    except Exception as e:
        print("❌ create-invoice exception:", e)
        return jsonify({"status": "error", "message": "Internal error"}), 500


@firestore.transactional
def _activate_pro(transaction, ref, order_id):
    snap = ref.get(transaction=transaction)
    cur = snap.to_dict() if snap.exists else {}
    if cur.get("last_order_id") == order_id:
        return False
    now_ms = int(time.time() * 1000)
    base = max(now_ms, int(cur.get("pro_until") or 0))
    update = {
        "is_pro": True,
        "subscription_status": "active",
        "pro_until": base + SUBSCRIPTION_DAYS * 86400 * 1000,
        "last_order_id": order_id,
    }
    if not cur.get("pro_since"):
        update["pro_since"] = now_ms
    transaction.set(ref, update, merge=True)
    return True


@app.route("/api/cryptomus-webhook", methods=["POST"])
def cryptomus_webhook():
    try:
        data = request.get_json(silent=True) or {}
        if not verify_webhook(data, request.headers.get("sign", "")):
            return jsonify({"error": "invalid signature"}), 403

        status = data.get("status")
        order_id = str(data.get("order_id", ""))

        if status in ("paid", "paid_over") and db and order_id.startswith("SUB."):
            body = order_id[len("SUB."):]
            user_id = body.rsplit(".", 1)[0] if "." in body else ""
            user_id = user_id or str(data.get("additional_data", ""))
            if user_id:
                ref = db.collection("users").document(user_id)
                _activate_pro(db.transaction(), ref, order_id)
            return jsonify({"status": "ok"})

        return jsonify({"status": "ignored"})
    except Exception as e:
        print("❌ Webhook error:", e)
        return jsonify({"error": "bad request"}), 400


# ============================================================
# 7. مصادر الأخبار
# ============================================================
RSS_FEEDS = {
    "ForexLive": "https://www.forexlive.com/feed/news",
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "DailyFX": "https://www.dailyfx.com/feeds/market-news",
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "Investing.com Central Banks": "https://www.investing.com/rss/news_14.rss",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Yahoo Finance Currency": "https://finance.yahoo.com/news/rssindex",
}

HIGH_WORDS = ["fed", "federal reserve", "interest rate", "cpi", "nfp", "powell", "inflation",
              "ecb", "central bank", "fomc", "gdp"]
MEDIUM_WORDS = ["dollar", "euro", "pound", "yen", "usd", "eur", "gbp", "jpy", "trade", "retail",
                "jobless", "treasury", "treasuries", "yield", "gold", "xau"]
EXTRA_WORDS = ["forex", "fx", "currency", "currencies", "btc"]

HIGH_RE = build_pattern(HIGH_WORDS, suffix=True)
MEDIUM_RE = build_pattern(MEDIUM_WORDS, suffix=True)
FOREX_RE = build_pattern(HIGH_WORDS + MEDIUM_WORDS + EXTRA_WORDS, suffix=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def generate_doc_id(title, source):
    return hashlib.sha256(f"{source}_{title}".encode("utf-8")).hexdigest()[:20]


def get_impact_level(title):
    if HIGH_RE.search(title):
        return "high"
    if MEDIUM_RE.search(title):
        return "medium"
    return "low"


def is_forex_news(title):
    return bool(FOREX_RE.search(title))


def fetch_single_feed(source_name, feed_url):
    items = []
    try:
        with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=8.0) as client:
            res = client.get(feed_url)
            if res.status_code != 200:
                return items
            feed = feedparser.parse(res.content)
            if feed.bozo and not feed.entries:
                return items
            for entry in feed.entries[:4]:
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
                    "link": entry.get("link", "#"),
                    "published": entry.get("published", entry.get("updated", "recent")),
                    "source": source_name,
                    "image": image_url,
                    "impact": get_impact_level(title),
                    "tags": extract_currencies_and_tags(title),
                    "sentiment": analyze_market_sentiment(title),
                    "target_timestamp": None,
                    "created_at_ms": int(time.time() * 1000),
                })
    except Exception as e:
        print(f"⚠ Feed {source_name} error: {e}")
    return items


def save_batch_chunked(items):
    CHUNK = 200
    for i in range(0, len(items), CHUNK):
        batch = db.batch()
        for item in items[i:i + CHUNK]:
            batch.set(db.collection("articles").document(item["doc_id"]), item, merge=True)
        batch.commit()


def fetch_calendar(current_time_ms):
    items = []
    try:
        with httpx.Client(headers=HEADERS, timeout=8.0) as client:
            cal_res = client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
            if cal_res.status_code != 200:
                return items
            for ev in cal_res.json():
                if ev.get("impact") not in ("High", "Medium"):
                    continue
                try:
                    dt = datetime.datetime.fromisoformat(ev.get("date", "").replace("Z", "+00:00"))
                    event_ts = int(dt.timestamp() * 1000)
                    if event_ts <= current_time_ms:
                        continue
                    news_title = f"{ev.get('country')} - {ev.get('title')}"
                    items.append({
                        "doc_id": generate_doc_id(f"{news_title}|{event_ts}", "Forex Factory Calendar"),
                        "title": news_title,
                        "link": "https://www.forexfactory.com/calendar",
                        "published": f"موعد الصدور: {ev.get('date')}",
                        "source": "Forex Factory Calendar",
                        "image": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=600&q=80",
                        "impact": "high" if ev.get("impact") == "High" else "medium",
                        "tags": [ev.get("country", "USD")],
                        "sentiment": "neutral",
                        "target_timestamp": event_ts,
                        "created_at_ms": current_time_ms,
                        "_forecast": str(ev.get("forecast", "")),
                        "_previous": str(ev.get("previous", "")),
                        "_date": str(ev.get("date", "")),
                    })
                except Exception:
                    continue
    except Exception as e:
        print("Calendar fetch error:", e)
    return items


# ============================================================
# 8. مهمة التحديث الرئيسية
# ============================================================
def update_news_cache_job():
    global _first_cycle
    ensure_preloaded()
    current_time_ms = int(time.time() * 1000)

    candidates = {}

    for item in fetch_calendar(current_time_ms):
        candidates[item["doc_id"]] = item

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(fetch_single_feed, n, u) for n, u in RSS_FEEDS.items()]
        for future in concurrent.futures.as_completed(futures):
            try:
                for item in future.result():
                    candidates.setdefault(item["doc_id"], item)
            except Exception as e:
                print("⚠️ Feed future error:", e)

    if not candidates:
        return

    new_items = [it for it in candidates.values() if not is_known(it["doc_id"])]

    saved_ok = True
    if db and new_items:
        try:
            to_save = [{k: v for k, v in it.items() if not k.startswith("_")} for it in new_items]
            save_batch_chunked(to_save)
        except Exception as e:
            saved_ok = False
            print("❌ Firestore Sync Error:", e)

    if saved_ok:
        suppress = _first_cycle and _suppress_first_notify
        for it in new_items:
            remember(it["doc_id"])
            if suppress:
                continue
            if it["source"] == "Forex Factory Calendar":
                if it["impact"] == "high":
                    send_high_impact_notification(
                        title=it["title"],
                        body=f"موعد الصدور المرتقب: {it['_date']}",
                        event_data={
                            "country": it["tags"][0] if it["tags"] else "",
                            "forecast": it["_forecast"],
                            "previous": it["_previous"],
                        },
                        is_pro_only=False,
                    )
            else:
                send_high_impact_notification(
                    title=it["title"],
                    body=f"المصدر: {it['source']}",
                    event_data={"link": it.get("link", "")},
                    is_pro_only=(it["impact"] != "high"),
                )

    _first_cycle = False
    print(f"⚡ [{datetime.datetime.now()}] Engine Refreshed: {len(new_items)} new items saved.")


# ============================================================
# 9. المجدول (تم التعديل إلى 10 ثوانٍ)
# ============================================================
_LOCK_FH = None


def acquire_scheduler_lock() -> bool:
    global _LOCK_FH
    try:
        import fcntl
        fh = open("/tmp/forex_scheduler.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FH = fh
        return True
    except ImportError:
        return True
    except OSError:
        return False


scheduler = None
if os.environ.get("ENABLE_SCHEDULER", "1") == "1" and acquire_scheduler_lock():
    scheduler = BackgroundScheduler()
    # تحديث الأخبار كل 10 ثوانٍ
    scheduler.add_job(update_news_cache_job, "interval", seconds=10,
                      max_instances=1, coalesce=True,
                      next_run_time=datetime.datetime.now())
    scheduler.add_job(cleanup_old_news_job, "interval", days=1, max_instances=1,
                      next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=2))
    scheduler.start()
    print("⏱️ Scheduler started in this process (Every 10 seconds).")


# ============================================================
# 10. API Endpoints
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "firebase": FIREBASE_READY, "storage": "firestore"})


@app.route("/", methods=["GET", "HEAD"])
def home():
    if request.method == "HEAD":
        return Response(status=200)
    news_list = []
    try:
        if db:
            ref = (
                db.collection("articles")
                .order_by("created_at_ms", direction=firestore.Query.DESCENDING)
                .limit(20)
                .stream()
            )
            news_list = [doc.to_dict() for doc in ref]
    except Exception as e:
        print("Firestore Fetch Error:", e)
    try:
        return render_template("index.html", news_list=news_list)
    except Exception:
        return jsonify({"status": "live", "message": "Pro Forex API Engine is running."})


@app.route("/api/news", methods=["GET"])
def get_forex_news():
    tag_filter = request.args.get("tag", "").upper()
    data = []
    
    if db:
        try:
            ref = (
                db.collection("articles")
                .order_by("created_at_ms", direction=firestore.Query.DESCENDING)
                .limit(40)
                .stream()
            )
            data = [d.to_dict() for d in ref]
        except Exception as e:
            print("Firestore news fetch error:", e)

    if tag_filter:
        data = [item for item in data if tag_filter in item.get("tags", [])]

    return jsonify({
        "status": "success",
        "total_results": len(data),
        "data": data,
        "checked_at": int(time.time() * 1000),
    })


@app.route("/api/user-status", methods=["GET"])
def check_user_status():
    user_id = get_request_uid(request.args.get("user_id", "").strip())
    if not user_id or not db:
        return jsonify({"is_pro": False})
    try:
        doc = db.collection("users").document(user_id).get()
        if doc.exists:
            d = doc.to_dict()
            is_pro = bool(d.get("is_pro", False))
            until = d.get("pro_until")
            if is_pro and until and int(until) < int(time.time() * 1000):
                is_pro = False
            return jsonify({"is_pro": is_pro, "pro_until": until})
    except Exception as e:
        print("user-status error:", e)
    return jsonify({"is_pro": False})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
