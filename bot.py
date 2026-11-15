import os
import json
import time
import hmac
import hashlib
import requests
from supabase import create_client
from google import genai
from google.genai import types

# ==========================================
# CONFIGURATION & GUARDRAILS
# ==========================================
# Testnet first! Switch to "https://api.india.delta.exchange" only after 7 days of testing.
DELTA_BASE = "https://cdn-ind.testnet.deltaex.org"
SYMBOL = "UNIUSD"
ORDER_SIZE = 1  # Number of UNIUSD contracts per strategy sleeve (keep small initially)

# Resolution to seconds mapping for candle lookback
RES_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# Initialize External Clients
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
ai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


# ==========================================
# DELTA EXCHANGE AUTHENTICATION & API
# ==========================================
def delta_request(method, endpoint, params=None, payload=None):
    """Executes signed REST API requests to Delta Exchange."""
    api_key = os.environ["DELTA_API_KEY"]
    api_secret = os.environ["DELTA_API_SECRET"]
    timestamp = str(int(time.time()))

    query_string = ""
    if params:
        query_string = "?" + "&".join([f"{k}={v}" for k, v in params.items()])

    body_str = json.dumps(payload) if payload else ""
    signature_data = f"{method}{timestamp}{endpoint}{query_string}{body_str}"
    signature = hmac.new(
        api_secret.encode("utf-8"),
        signature_data.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "api-key": api_key,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json",
        "User-Agent": "uniusd-autonomous-agent"
    }

    url = f"{DELTA_BASE}{endpoint}{query_string}"
    response = requests.request(method, url, headers=headers, data=body_str, timeout=15)
    return response.json()


def get_uniusd_product_id():
    """Fetches the live numeric product_id for UNIUSD perpetual contract."""
    res = requests.get(f"{DELTA_BASE}/v2/products/{SYMBOL}", timeout=10).json()
    return res["result"]["id"]


def get_uniusd_candles(resolution="15m", limit=50):
    """Fetches historical OHLCV candles for UNIUSD."""
    end = int(time.time())
    start = end - (limit * RES_SECONDS[resolution])
    url = f"{DELTA_BASE}/v2/history/candles"
    params = {"symbol": SYMBOL, "resolution": resolution, "start": start, "end": end}
    res = requests.get(url, params=params, timeout=10).json()
    return res.get("result", [])


# ==========================================
# TELEGRAM NOTIFICATIONS
# ==========================================
def send_telegram(message):
    """Sends formatted alerts to your Telegram chat."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram notification failed: {e}")


# ==========================================
# RISK GUARDRAILS & SUPABASE MEMORY
# ==========================================
def has_open_trade_for_strategy(strategy_type):
    """GUARDRAIL 1: Prevents duplicate or conflicting orders inside the same strategy sleeve."""
    res = (
        supabase.table("trades")
        .select("id")
        .eq("strategy_type", strategy_type)
        .eq("status", "OPEN")
        .execute()
    )
    return len(res.data) > 0


def get_past_lessons(strategy_type):
    """Retrieves past losing trades and AI lessons for self-improvement."""
    res = (
        supabase.table("trades")
        .select("side, entry_price, stop_loss, take_profit, setup_reasoning, pnl, ai_lesson")
        .eq("strategy_type", strategy_type)
        .eq("status", "CLOSED")
        .lt("pnl", 0)
        .order("created_at", desc=True)
        .limit(5)
        .execute()
    )
    return res.data


def validate_risk_parameters(side, entry, sl, tp):
    """GUARDRAIL 2: Enforces valid SL/TP direction and minimum 1:1.5 Risk-to-Reward ratio."""
    if entry <= 0 or sl <= 0 or tp <= 0:
        return False, "Invalid zero or negative prices."

    if side == "BUY":
        if not (sl < entry < tp):
            return False, f"BUY invalid levels: SL({sl}) < Entry({entry}) < TP({tp}) violated."
        risk = entry - sl
        reward = tp - entry
    elif side == "SELL":
        if not (tp < entry < sl):
            return False, f"SELL invalid levels: TP({tp}) < Entry({entry}) < SL({sl}) violated."
        risk = sl - entry
        reward = entry - tp
    else:
        return False, "Invalid trade side."

    # Enforce minimum 1.5 R:R and cap stop-loss distance at 8% max
    if (risk / entry) > 0.08:
        return False, f"Stop-loss too wide ({round((risk/entry)*100, 2)}% > 8% max)."
    if reward < (1.5 * risk):
        return False, f"Poor Risk:Reward ({round(reward/risk, 2)} < 1.5 minimum)."

    return True, "Valid"


# ==========================================
# SELF-IMPROVEMENT SYNC (CLOSED TRADES)
# ==========================================
def sync_closed_trades(product_id):
    """Checks if open trades in Supabase have closed on Delta Exchange, calculates PnL, and logs AI lesson."""
    open_trades = supabase.table("trades").select("*").eq("status", "OPEN").execute().data
    if not open_trades:
        return

    # Fetch recent fills/orders from Delta Exchange
    fills_res = delta_request("GET", "/v2/fills", params={"product_id": product_id, "page_size": 50})
    fills = fills_res.get("result", [])

    # Check current live position on Delta
    pos_res = delta_request("GET", "/v2/positions", params={"product_id": product_id})
    current_size = pos_res.get("result", {}).get("size", 0)

    # If net exchange position is flat (0), any open trade in Supabase was stopped out or hit TP
    if current_size == 0 and fills:
        latest_exit_price = float(fills[0].get("price", 0))

        for trade in open_trades:
            entry = float(trade["entry_price"])
            side = trade["side"]
            pnl = (latest_exit_price - entry) * ORDER_SIZE if side == "BUY" else (entry - latest_exit_price) * ORDER_SIZE
            pnl = round(pnl, 4)

            # Ask Gemini to reflect on why this trade won or lost
            reflection_prompt = f"""
            Analyze this completed UNIUSD {trade['strategy_type']} trade:
            Side: {side} | Entry: {entry} | Exit: {latest_exit_price} | PnL: {pnl}
            Original Setup Reasoning: {trade['setup_reasoning']}
            
            Write a concise 2-sentence rule/lesson to improve future {trade['strategy_type']} entries and avoid losses.
            """
            lesson_resp = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=reflection_prompt
            )
            ai_lesson = lesson_resp.text.strip()

            # Update Supabase record
            supabase.table("trades").update({
                "status": "CLOSED",
                "pnl": pnl,
                "ai_lesson": ai_lesson
            }).eq("id", trade["id"]).execute()

            emoji = "✅" if pnl > 0 else "❌"
            send_telegram(
                f"{emoji} *UNIUSD {trade['strategy_type']} Closed*\n"
                f"• *Side:* {side}\n"
                f"• *Entry:* ${entry} → *Exit:* ${latest_exit_price}\n"
                f"• *Realized PnL:* ${pnl}\n"
                f"• *AI Lesson:* _{ai_lesson}_"
            )


# ==========================================
# CORE TRADING ENGINE
# ==========================================
def analyze_and_trade(product_id, strategy_type, resolution):
    """Evaluates UNIUSD candles, applies past lessons, checks guardrails, and executes bracket orders."""
    if has_open_trade_for_strategy(strategy_type):
        print(f"[{strategy_type}] Skipping: Active OPEN trade already exists.")
        return

    candles = get_uniusd_candles(resolution, limit=50)
    if len(candles) < 20:
        print(f"[{strategy_type}] Insufficient candle data.")
        return

    current_price = float(candles[-1]["close"])
    past_mistakes = get_past_lessons(strategy_type)

    prompt = f"""
    You are an elite autonomous crypto futures trader trading ONLY UNIUSD.
    Current Mode: {strategy_type} Trader (Chart Timeframe: {resolution})
    Current UNIUSD Price: {current_price}
    Recent 20 OHLCV Candles (oldest to newest): {json.dumps(candles[-20:])}
    
    Past Losing Setups & Lessons Learned (DO NOT repeat these mistakes):
    {json.dumps(past_mistakes)}
    
    Task:
    Analyze trend, momentum, support/resistance, and volume.
    Only take high-probability setups with at least a 1:1.5 Risk-to-Reward ratio. Otherwise output HOLD.
    
    Respond strictly in valid JSON with this exact schema:
    {{
      "action": "BUY" | "SELL" | "HOLD",
      "entry": {current_price},
      "sl": float,
      "tp": float,
      "reason": "concise technical reasoning in 1-2 sentences"
    }}
    """

    response = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )

    try:
        decision = json.loads(response.text)
    except json.JSONDecodeError:
        print(f"[{strategy_type}] Failed to parse AI JSON.")
        return

    action = decision.get("action", "HOLD").upper()
    if action not in ["BUY", "SELL"]:
        print(f"[{strategy_type}] Decision: HOLD ({decision.get('reason')})")
        return

    sl = round(float(decision["sl"]), 4)
    tp = round(float(decision["tp"]), 4)

    # Run Guardrail 2: Risk-to-Reward & Sanity Check
    is_valid, msg = validate_risk_parameters(action, current_price, sl, tp)
    if not is_valid:
        print(f"[{strategy_type}] Trade blocked by risk guardrail: {msg}")
        return

    # GUARDRAIL 3: Place Market Order with Native Bracket Stop-Loss & Take-Profit
    order_payload = {
        "product_id": product_id,
        "size": ORDER_SIZE,
        "side": "buy" if action == "BUY" else "sell",
        "order_type": "market_order",
        "bracket_stop_loss_price": str(sl),
        "bracket_take_profit_price": str(tp),
        "bracket_stop_trigger_method": "last_traded_price"
    }

    order_res = delta_request("POST", "/v2/orders", payload=order_payload)

    if order_res.get("success"):
        # Log trade setup in Supabase
        supabase.table("trades").insert({
            "timeframe": resolution,
            "strategy_type": strategy_type,
            "side": action,
            "entry_price": current_price,
            "stop_loss": sl,
            "take_profit": tp,
            "setup_reasoning": decision["reason"],
            "status": "OPEN"
        }).execute()

        # Notify via Telegram
        send_telegram(
            f"🚀 *New UNIUSD {strategy_type} Trade*\n"
            f"• *Action:* {action} ({ORDER_SIZE} contracts)\n"
            f"• *Entry:* ${current_price}\n"
            f"• *Stop Loss:* ${sl}\n"
            f"• *Take Profit:* ${tp}\n"
            f"• *Timeframe:* {resolution}\n"
            f"• *Setup:* {decision['reason']}"
        )
        print(f"[{strategy_type}] {action} executed successfully.")
    else:
        error_info = order_res.get("error", order_res)
        print(f"[{strategy_type}] Delta Order Failed: {error_info}")
        send_telegram(f"⚠️ *UNIUSD {strategy_type} Order Error:* `{error_info}`")


if __name__ == "__main__":
    try:
        uniusd_id = get_uniusd_product_id()
        # 1. First sync closed trades & generate self-improvement lessons
        sync_closed_trades(uniusd_id)

        # 2. Scan UNIUSD across all 4 trading styles
        strategies = [
            ("Scalper", "15m"),
            ("Day", "1h"),
            ("Swing", "4h"),
            ("Position", "1d")
        ]
        for style, timeframe in strategies:
            analyze_and_trade(uniusd_id, style, timeframe)
            time.sleep(2)  # Rate-limit buffer between Gemini/Delta calls

    except Exception as e:
        print(f"Critical Agent Error: {e}")
        send_telegram(f"🚨 *Agent Runtime Error:* `{str(e)}`")
