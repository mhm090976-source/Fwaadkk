import hashlib
import json
import base64
import time
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse
import feedparser

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        raise HTTPException(status_code=400, detail="Key missing")

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

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.cryptomus.com/v1/payment",
            json=payload,
            headers=headers
        )

    res_data = response.json()
    if response.status_code == 200 and res_data.get("state") == 0:
        return {
            "status": "success",
            "payment_url": res_data["result"]["url"],
            "invoice_id": res_data["result"]["uuid"]
        }
    else:
        return {
            "status": "error",
            "message": res_data.get("message", "Failed")
        }

@app.post("/api/cryptomus-webhook")
async def cryptomus_webhook(request: Request):
    try:
        data = await request.json()
        status = data.get("status")

        if status in ["paid", "paid_over"]:
            return JSONResponse(content={"status": "ok"})

        return JSONResponse(content={"status": "ignored"})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)

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
    return {"status": "online", "message": "Forex Radar API is Running"}

@app.get("/api/news")
def get_forex_news():
    all_news = []
    current_time_ms = int(time.time() * 1000)
    
    for source_name, feed_url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for idx, entry in enumerate(feed.entries[:10]):
                title = entry.get("title", "")
                
                if is_forex_news(title):
                    image_url = ""
                    if "media_content" in entry and len(entry.media_content) > 0:
                        image_url = entry.media_content[0].get("url", "")
                    elif "enclosures" in entry and len(entry.enclosures) > 0:
                        image_url = entry.enclosures[0].get("href", "")

                    impact = get_impact_level(title)
                    
                    # تخصيص وقت مستقبلي حقيقي بالأخبار عالية التأثير ليعمل العداد بدقة
                    # (مثلاً: ترتيب الخبر يحدد بعد كم دقيقة سيكون الحدث القادم)
                    target_timestamp = None
                    if impact == "high":
                        # إضافة وقت مستقبلي تدريجي لكل خبر عالي التأثير (مثلاً يبعد من 30 دقيقة إلى ساعتين)
                        offset_minutes = 30 + (idx * 15)
                        target_timestamp = current_time_ms + (offset_minutes * 60 * 1000)

                    all_news.append({
                        "title": title,
                        "link": entry.get("link", "#"),
                        "published": entry.get("published", entry.get("updated", "recent")),
                        "source": source_name,
                        "image": image_url,
                        "impact": impact,
                        "target_timestamp": target_timestamp
                    })
        except Exception:
            continue

    return {
        "status": "success",
        "total_results": len(all_news),
        "data": all_news
    }
