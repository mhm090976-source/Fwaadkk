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
from email.utils import parsedate_to_datetime

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
# 2. إدارة معرفات الأخبار، الذاكرة الساخنة، وسجل الإشعارات السريع
# ============================================================
MAX_KNOWN = 2000
KNOWN_IDS = set()
KNOWN_QUEUE = deque()

MAX_SENT_NOTIF = 2000
SENT_NOTIF_CACHE = set()
SENT_NOTIF_QUEUE = deque()

HOT_NEWS_RAM = deque(maxlen=120)
_HOT_LOCK = threading.Lock()
_CALENDAR_CACHE = []
_CAL_LOCK = threading.Lock()

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


def push_hot_news(items):
    if not items:
        return
    with _HOT_LOCK:
        existing_ids = {x.get("doc_id") for x in HOT_NEWS_RAM}
        for it in items:
            clean = {k: v for k, v in it.items() if not k.startswith("_")}
            if clean.get("doc_id") not in existing_ids:
                HOT_NEWS_RAM.appendleft(clean)
                existing_ids.add(clean.get("doc_id"))


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
            .limit(500)
            .select([])
            .stream()
        )
        for d in docs:
            remember(d.id)

        hot_docs = (
            db.collection("articles")
            .order_by("created_at_ms", direction=firestore.Query.DESCENDING)
            .limit(80)
            .stream()
        )
        with _HOT_LOCK:
            HOT_NEWS_RAM.clear()
            for hd in hot_docs:
                HOT_NEWS_RAM.append(hd.to_dict())
    except Exception as e:
        print("⚠️ Preload known ids failed:", e)
    _suppress_first_notify = len(KNOWN_IDS) == 0


def was_notification_sent(notification_key: str) -> bool:
    if notification_key in SENT_NOTIF_CACHE:
        return True
    if not db:
        return False
    try:
        doc = db.collection("sent_notifications").document(notification_key).get()
        if doc.exists:
            SENT_NOTIF_CACHE.add(notification_key)
            SENT_NOTIF_QUEUE.append(notification_key)
            return True
        return False
    except Exception:
        return False


def mark_notification_sent(notification_key: str):
    if notification_key not in SENT_NOTIF_CACHE:
        SENT_NOTIF_CACHE.add(notification_key)
        SENT_NOTIF_QUEUE.append(notification_key)
        while len(SENT_NOTIF_QUEUE) > MAX_SENT_NOTIF:
            SENT_NOTIF_CACHE.discard(SENT_NOTIF_QUEUE.popleft())

    def _write():
        if not db:
            return
        try:
            db.collection("sent_notifications").document(notification_key).set({
                "sent_at_ms": int(time.time() * 1000)
            })
        except Exception as e:
            print(f"⚠️ Error marking notification sent: {e}")

    threading.Thread(target=_write, daemon=True).start()


# ============================================================
# 3. التحليل المتقدم وتحويل التوقيتات بدقة (UTC Epoch)
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


def parse_entry_timestamp(entry) -> int:
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            dt = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
            return int(dt.timestamp() * 1000)

        pub_str = entry.get("published", entry.get("updated", ""))
        if pub_str:
            dt = parsedate_to_datetime(pub_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            else:
                dt = dt.astimezone(datetime.timezone.utc)
            return int(dt.timestamp() * 1000)
    except Exception:
        pass
    return int(time.time() * 1000)


# ============================================================
# 4. إشعارات FCM الفورية
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
            "timestamp": str(int(time.time() * 1000)),
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
        messaging.send(message)
    except Exception as e:
        print(f"❌ FCM Notification Error: {e}")


# ============================================================
# 5. تنظيف قاعدة البيانات
# ============================================================
def cleanup_old_news_job():
    if not db:
        return
    t0 = time.perf_counter()
    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
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
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if deleted:
            print(f"🧹 Auto-Cleanup: Deleted {deleted} old news items in {elapsed_ms:.1f} ms.")
        else:
            print(f"🧹 Auto-Cleanup finished in {elapsed_ms:.1f} ms (0 deleted).")
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"❌ Error during cleanup after {elapsed_ms:.1f} ms: {e}")


# ============================================================
# 6. مهمة فحص التقويم قبل 5 دقائق
# ============================================================
def check_calendar_5min_job():
    if not db:
        return
    t0 = time.perf_counter()
    try:
        now_ms = int(time.time() * 1000)
        lower_limit = now_ms + 270 * 1000
        upper_limit = now_ms + 330 * 1000
        docs = (
            db.collection("articles")
            .where("target_timestamp", ">=", lower_limit)
            .where("target_timestamp", "<=", upper_limit)
            .stream()
        )
        for doc in docs:
            d = doc.to_dict()
            doc_id = doc.id
            notif_key = f"cal_5min_{doc_id}"

            if was_notification_sent(notif_key):
                continue

            if d.get("source") == "Forex Factory Calendar":
                mark_notification_sent(notif_key)
                title = d.get("title", "")
                send_high_impact_notification(
                    title="⏳ تذكير التقويم الاقتصادي",
                    body=f"يتبقى 5 دقائق على صدور الحدث: {title}",
                    event_data={
                        "show_overlay": "true",
                        "type": "calendar_5min",
                        "title": title,
                        "body": f"يتبقى 5 دقائق على صدور الحدث: {title}",
                    },
                    is_pro_only=False,
                )
                print(f"⏰ Server sent 5-min calendar alert for: {title}")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"⏱️ check_calendar_5min_job completed in {elapsed_ms:.1f} ms.")
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"❌ Calendar 5min check error after {elapsed_ms:.1f} ms: {e}")


# ============================================================
# 7. Cryptomus والدفع
# ============================================================
@app.route("/cryptomus_23a84c54.html", methods=["GET"])
def cryptomus_verification():
    return Response("cryptomus=23a84c54", mimetype="text/plain")


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
# 8. مصادر الأخبار (عميل HTTP مشترك وسريع)
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

HTTP_CLIENT = httpx.Client(
    headers=HEADERS,
    follow_redirects=True,
    timeout=3.0,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


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
    t0 = time.perf_counter()
    try:
        res = HTTP_CLIENT.get(feed_url)
        if res.status_code != 200:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"⚠ Feed {source_name} HTTP {res.status_code} in {elapsed_ms:.1f} ms")
            return items
        feed = feedparser.parse(res.content)
        if feed.bozo and not feed.entries:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"⚠ Feed {source_name} parse error in {elapsed_ms:.1f} ms: {getattr(feed, 'bozo_exception', '')}")
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

            content_text = ""
            if entry.get("content"):
                content_text = entry.content[0].get("value", "")
            elif entry.get("summary"):
                content_text = entry.summary
            elif entry.get("description"):
                content_text = entry.description

            pub_ms = parse_entry_timestamp(entry)
            arrival_ms = int(time.time() * 1000)

            items.append({
                "doc_id": generate_doc_id(title, source_name),
                "title": title,
                "link": entry.get("link", "#"),
                "published": entry.get("published", entry.get("updated", "recent")),
                "source": source_name,
                "image": image_url,
                "content": content_text,
                "impact": get_impact_level(title),
                "tags": extract_currencies_and_tags(title),
                "sentiment": analyze_market_sentiment(title),
                "target_timestamp": None,
                "created_at_ms": pub_ms,
                "server_received_at_ms": arrival_ms,
            })
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"⚠ Feed {source_name} error after {elapsed_ms:.1f} ms: {e}")
    return items


def save_batch_chunked(items):
    if not db or not items:
        return
    CHUNK = 200
    for i in range(0, len(items), CHUNK):
        batch = db.batch()
        for item in items[i:i + CHUNK]:
            batch.set(db.collection("articles").document(item["doc_id"]), item, merge=True)
        batch.commit()


def save_async_firestore(items):
    if not db or not items:
        return

    def _run():
        t0 = time.perf_counter()
        try:
            to_save = [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]
            save_batch_chunked(to_save)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"💾 Async Firestore Sync ({len(to_save)} items) completed in {elapsed_ms:.1f} ms.")
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"❌ Async Firestore Sync Error after {elapsed_ms:.1f} ms: {e}")

    threading.Thread(target=_run, daemon=True).start()


# --- دالة جلب التقويم المحدثة لتغطي الأسبوع كاملاً (من السبت إلى الجمعة) ---
def fetch_calendar(current_time_ms=None):
    if current_time_ms is None:
        current_time_ms = int(time.time() * 1000)
    items = []
    t0 = time.perf_counter()
    try:
        cal_res = HTTP_CLIENT.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=6.0)
        if cal_res.status_code != 200:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"Calendar fetch HTTP {cal_res.status_code} in {elapsed_ms:.1f} ms")
            return items

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        days_since_saturday = (now_dt.weekday() - 5) % 7
        saturday_start = (now_dt - datetime.timedelta(days=days_since_saturday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        friday_end = saturday_start + datetime.timedelta(days=6, hours=23, minutes=59, seconds=59)

        saturday_ms = int(saturday_start.timestamp() * 1000)
        friday_ms = int(friday_end.timestamp() * 1000)

        for ev in cal_res.json():
            if ev.get("impact") not in ("High", "Medium"):
                continue
            try:
                dt = datetime.datetime.fromisoformat(ev.get("date", "").replace("Z", "+00:00"))
                event_ts = int(dt.timestamp() * 1000)

                if not (saturday_ms <= event_ts <= friday_ms):
                    continue

                news_title = f"{ev.get('country')} - {ev.get('title')}"
                day_name = dt.strftime("%A")
                date_str = dt.strftime("%Y-%m-%d")

                items.append({
                    "doc_id": generate_doc_id(f"{news_title}|{event_ts}", "Forex Factory Calendar"),
                    "title": news_title,
                    "link": "https://www.forexfactory.com/calendar",
                    "published": f"موعد الصدور: {ev.get('date')}",
                    "source": "Forex Factory Calendar",
                    "image": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=600&q=80",
                    "content": f"الحدث الاقتصادي: {news_title} | المتوقع: {ev.get('forecast', 'N/A')} | السابق: {ev.get('previous', 'N/A')}",
                    "impact": "high" if ev.get("impact") == "High" else "medium",
                    "tags": [ev.get("country", "USD"), day_name],
                    "sentiment": "neutral",
                    "target_timestamp": event_ts,
                    "created_at_ms": current_time_ms,
                    "server_received_at_ms": current_time_ms,
                    "calendar_date": date_str,
                    "calendar_day": day_name,
                    "_forecast": str(ev.get("forecast", "")),
                    "_previous": str(ev.get("previous", "")),
                    "_date": str(ev.get("date", "")),
                })
            except Exception as ev_err:
                print(f"Calendar event parse error: {ev_err}")
                continue
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"Calendar fetch error after {elapsed_ms:.1f} ms: {e}")
    return items


def refresh_calendar_job():
    t0 = time.perf_counter()
    try:
        ensure_preloaded()
        now_ms = int(time.time() * 1000)
        cal_items = fetch_calendar(now_ms)
        if not cal_items:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"📅 refresh_calendar_job finished in {elapsed_ms:.1f} ms (0 items).")
            return
        with _CAL_LOCK:
            _CALENDAR_CACHE.clear()
            _CALENDAR_CACHE.extend(cal_items)

        new_cal = [it for it in cal_items if not is_known(it["doc_id"])]
        if new_cal:
            push_hot_news(new_cal)
            save_async_firestore(new_cal)
            for it in new_cal:
                remember(it["doc_id"])
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"📅 refresh_calendar_job completed in {elapsed_ms:.1f} ms ({len(new_cal)} new).")
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"❌ refresh_calendar_job error after {elapsed_ms:.1f} ms: {e}")


# ============================================================
# 9. مهمة التحديث السريعة (كل 10 ثوانٍ) مع إرسال فوري لـ FCM
# ============================================================
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=len(RSS_FEEDS))


def update_news_cache_job():
    global _first_cycle
    t0 = time.perf_counter()
    try:
        ensure_preloaded()
        current_time_ms = int(time.time() * 1000)

        candidates = {}
        with _CAL_LOCK:
            for item in _CALENDAR_CACHE:
                candidates[item["doc_id"]] = item

        futures = {EXECUTOR.submit(fetch_single_feed, n, u): n for n, u in RSS_FEEDS.items()}
        for future in concurrent.futures.as_completed(futures, timeout=4.5):
            try:
                for item in future.result():
                    title_lower = item["title"].lower()
                    is_duplicate = False
                    for existing in candidates.values():
                        if (
                            existing["source"] != "Forex Factory Calendar"
                            and abs(existing["created_at_ms"] - item["created_at_ms"]) < 1800000
                        ):
                            sim_words = set(title_lower.split()).intersection(set(existing["title"].lower().split()))
                            if len(sim_words) >= 4:
                                is_duplicate = True
                                break
                    if not is_duplicate:
                        candidates.setdefault(item["doc_id"], item)
            except concurrent.futures.TimeoutError:
                print(f"⚠️ Feed future timeout for {futures.get(future, 'unknown')}")
                continue
            except Exception as e:
                print("⚠️ Feed future error:", e)

        if not candidates:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f"⏱️ update_news_cache_job finished in {elapsed_ms:.1f} ms (no candidates).")
            return

        new_items = [it for it in candidates.values() if not is_known(it["doc_id"])]
        if not new_items:
            _first_cycle = False
            return

        push_hot_news(new_items)
        save_async_firestore(new_items)

        suppress = _first_cycle and _suppress_first_notify
        for it in new_items:
            remember(it["doc_id"])
            if suppress:
                continue

            news_age_ms = current_time_ms - it.get("created_at_ms", current_time_ms)
            if news_age_ms > 300000:
                continue

            if it["source"] == "Forex Factory Calendar":
                if it["impact"] == "high":
                    notif_key = f"cal_pub_{it['doc_id']}"
                    if not was_notification_sent(notif_key):
                        mark_notification_sent(notif_key)
                        send_high_impact_notification(
                            title=it["title"],
                            body=f"موعد الصدور المرتقب: {it['_date']}",
                            event_data={
                                "doc_id": it["doc_id"],
                                "source": it["source"],
                                "impact": it["impact"],
                                "sentiment": it["sentiment"],
                                "tags": json.dumps(it.get("tags", []), ensure_ascii=False),
                                "published": it.get("published", ""),
                                "country": it["tags"][0] if it["tags"] else "",
                                "forecast": it.get("_forecast", ""),
                                "previous": it.get("_previous", ""),
                            },
                            is_pro_only=False,
                        )
            else:
                notif_key = f"news_{it['doc_id']}"
                if not was_notification_sent(notif_key):
                    mark_notification_sent(notif_key)
                    send_high_impact_notification(
                        title=it["title"],
                        body=f"المصدر: {it['source']}",
                        event_data={
                            "doc_id": it["doc_id"],
                            "link": it.get("link", ""),
                            "source": it.get("source", ""),
                            "impact": it.get("impact", "low"),
                            "sentiment": it.get("sentiment", "neutral"),
                            "tags": json.dumps(it.get("tags", []), ensure_ascii=False),
                            "published": it.get("published", ""),
                        },
                        is_pro_only=(it["impact"] != "high"),
                    )

        _first_cycle = False
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(
            f"⚡ [{datetime.datetime.now()}] Engine Refreshed in {elapsed_ms:.1f} ms: "
            f"{len(new_items)} new items dispatched."
        )
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"❌ update_news_cache_job error after {elapsed_ms:.1f} ms: {e}")


# ============================================================
# 10. المجدول (معدل التحديث 10 ثوانٍ للأخبار)
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
    scheduler.add_job(
        update_news_cache_job,
        "interval",
        seconds=10,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.datetime.now(),
    )
    scheduler.add_job(
        refresh_calendar_job,
        "interval",
        minutes=10,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.datetime.now(),
    )
    scheduler.add_job(
        check_calendar_5min_job,
        "interval",
        seconds=60,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        cleanup_old_news_job,
        "interval",
        days=1,
        max_instances=1,
        next_run_time=datetime.datetime.now() + datetime.timedelta(minutes=2),
    )
    scheduler.start()
    print("⏱️ Scheduler started in this process (10s news interval).")


# ============================================================
# 11. API Endpoints
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "firebase": FIREBASE_READY, "storage": "firestore+ram"})


@app.route("/", methods=["GET", "HEAD"])
def home():
    if request.method == "HEAD":
        return Response(status=200)
    news_list = []
    with _HOT_LOCK:
        news_list = list(HOT_NEWS_RAM)[:20]
    if not news_list and db:
        try:
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
    after_ms_raw = request.args.get("after_ms", "")
    after_ms = int(after_ms_raw) if after_ms_raw.isdigit() else 0

    with _HOT_LOCK:
        data = list(HOT_NEWS_RAM)[:40]

    if not data and db:
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

    if after_ms > 0:
        data = [item for item in data if int(item.get("created_at_ms", 0)) > after_ms]

    if tag_filter:
        data = [item for item in data if tag_filter in item.get("tags", [])]

    return jsonify({
        "status": "success",
        "total_results": len(data),
        "data": data,
        "checked_at": int(time.time() * 1000),
    })


@app.route("/api/article/<doc_id>", methods=["GET"])
def get_article_detail(doc_id):
    with _HOT_LOCK:
        for item in HOT_NEWS_RAM:
            if item.get("doc_id") == doc_id:
                return jsonify({"status": "success", "data": item})

    if not db:
        return jsonify({"status": "error", "message": "Database not ready"}), 500
    try:
        doc = db.collection("articles").document(doc_id).get()
        if doc.exists:
            return jsonify({"status": "success", "data": doc.to_dict()})
        return jsonify({"status": "error", "message": "Article not found"}), 404
    except Exception as e:
        print(f"Article detail error: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


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
