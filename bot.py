import os
import time
import hmac
import hashlib
import requests
from supabase import create_client
from google import genai

# 1. Initialize Clients
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
ai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
DELTA_BASE = "https://api.india.delta.exchange" # Use api.delta.exchange if global

def send_telegram(message):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    requests.post(url, json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": message})

def get_uniusd_candles(resolution="15m", limit=50):
    end = int(time.time())
    start = end - (limit * 900) # Adjust multiplier based on resolution
    url = f"{DELTA_BASE}/v2/history/candles?symbol=UNIUSD&resolution={resolution}&start={start}&end={end}"
    return requests.get(url).json().get("result", [])

def get_past_lessons(strategy_type):
    response = supabase.table("trades").select("setup_reasoning, pnl, ai_lesson") \
        .eq("strategy_type", strategy_type).eq("status", "CLOSED").lt("pnl", 0) \
        .order("created_at", desc=True).limit(5).execute()
    return response.data

def analyze_and_trade(strategy_type, resolution):
    candles = get_uniusd_candles(resolution)
    past_mistakes = get_past_lessons(strategy_type)
    
    prompt = f"""
    You are an autonomous UNIUSD {strategy_type} trader.
    Recent {resolution} candles: {candles[-15:]}
    Past losing trades & lessons to avoid: {past_mistakes}
    
    Decide: BUY, SELL, or HOLD. Return strictly JSON:
    {{"action": "BUY/SELL/HOLD", "entry": float, "sl": float, "tp": float, "reason": "short explanation"}}
    """
    decision = ai_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    # Parse JSON, execute signed order on Delta Exchange if BUY/SELL,
    # insert into Supabase 'trades' table, and call send_telegram().

def sync_closed_trades():
    # Fetch closed orders/positions from Delta Exchange for UNIUSD
    # Update 'status' to CLOSED and 'pnl' in Supabase
    # Generate 'ai_lesson' via Gemini for any newly closed trade and update Supabase
    pass

if __name__ == "__main__":
    sync_closed_trades()
    for style, tf in [("Scalper", "5m"), ("Day", "1h"), ("Swing", "4h"), ("Position", "1d")]:
        analyze_and_trade(style, tf)
