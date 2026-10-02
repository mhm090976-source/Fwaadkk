import hashlib
import json
import base64
import time
import datetime
import httpx
import feedparser
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse
import firebase_admin
from firebase_admin import firestore

app = FastAPI(title="Forex Radar API")

# إعدادات CORS المدمجة في FastAPI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 1. تهيئة Firebase ---
db = None
try:
    firebase_admin.initialize_app()
    db = firestore.client()
    print("Firebase initialized successfully.")
except Exception as e:
    print("Firebase initialization skipped or failed:", e)

# --- 2. إعدادات Cryptomus ---
CRYPTOMUS_MERCHANT_ID = "b9c7b8dd-cc24-4c13-beef-1f97ae33f932"
CRYPTOMUS_PAYMENT_KEY = "YOUR_PAYMENT_API_KEY_HERE"

@app.get("/cryptomus_b9c7b8dd.html", response_class=PlainTextResponse)
def cryptomus_verification():
    return "cryptomus=b9c7b8dd"

def generate_cryptomus_signature(data_dict: dict, api_key: str) -> str:
    json_data = json.dumps(data_dict, separators=(',', ':')).encode('utf-8')
    base64_data = base64.b64encode(json_data).decode('utf-8')
    sign_str = base64_data + api_key
    return hashlib.md5(sign_str.encode('utf-8')).hexdigest()

@app.post("/api/create-invoice")
async def create_subscription_invoice(amount: str = "10.00", order_id: str = "SUB_1001"):
    if CRYPTOMUS_PAYMENT_KEY == "YOUR_PAYMENT_API_KEY_HERE":
        raise HTTPException(status_code=400, detail="Cryptomus Payment Key is missing")

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
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.cryptomus.com/v1/payment",
                json=payload,
                headers=headers,
                timeout=10.0
            )

        res_data = response.json()
        if response.status_code == 200 and res_data.get("state") == 0:
            return {
                "status": "success",
                "payment_url": res_data["result"]["url"],
                "invoice_id": res_data["result"]["uuid"]
            }
        return {"status": "error", "message": res_data.get("message", "Failed")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cryptomus-webhook")
async def cryptomus_webhook(request: Request):
    try:
        data = await request.json()
        status = data.get("status")
        if status in ["paid", "paid_over"]:
            return {"status": "ok"}
        return {"status": "ignored"}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

# --- 3. إعدادات الأخبار والتقويم ---
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

@app.get("/")
def home():
    news_list = []
    try:
        if db:
            articles_ref = db.collection('articles').stream()
            news_list = [doc.to_dict() for doc in articles_ref]
    except Exception as e:
        print("Firestore Fetch Error:", e)

    return {"status": "online", "message": "Forex Radar API is Running", "database_articles": news_list}

@app.get("/api/news")
async def get_forex_news():
    all_news = []
    current_time_ms = int(time.time() * 1000)

    try:
        async with httpx.AsyncClient() as client:
            cal_res = await client.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=5.0)
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

    return {
        "status": "success",
        "total_results": len(all_news),
        "data": all_news
    }
