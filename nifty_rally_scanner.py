#!/usr/bin/env python3
"""
Nifty 50 Rally Scanner - Real-time Momentum Detector
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "pandas"])
    import pandas as pd

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

RALLY_MOVE_PCT = 1.0
RALLY_VOLUME_MULT = 2.0
RALLY_LOOKBACK_CANDLES = 3
VOLUME_BASELINE_CANDLES = 6
SL_PCT = 0.005
TARGET_PCT = 0.015

STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "alerted_today.json"

NEWS_FEEDS = [
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/markets.xml",
]

NIFTY_50 = [
    "ADANIENT.NS", "ADANIPORTS.NS", "APOLLOHOSP.NS", "ASIANPAINT.NS",
    "AXISBANK.NS", "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS",
    "BPCL.NS", "BHARTIARTL.NS", "BRITANNIA.NS", "CIPLA.NS",
    "COALINDIA.NS", "DIVISLAB.NS", "DRREDDY.NS", "EICHERMOT.NS",
    "GRASIM.NS", "HCLTECH.NS", "HDFCBANK.NS", "HDFCLIFE.NS",
    "HEROMOTOCO.NS", "HINDALCO.NS", "HINDUNILVR.NS", "ICICIBANK.NS",
    "ITC.NS", "INDUSINDBK.NS", "INFY.NS", "JSWSTEEL.NS",
    "KOTAKBANK.NS", "LT.NS", "M&M.NS", "MARUTI.NS",
    "NTPC.NS", "NESTLEIND.NS", "ONGC.NS", "POWERGRID.NS",
    "RELIANCE.NS", "SBILIFE.NS", "SBIN.NS", "SUNPHARMA.NS",
    "TCS.NS", "TATACONSUM.NS", "TATAMOTORS.NS", "TATASTEEL.NS",
    "TECHM.NS", "TITAN.NS", "TRENT.NS", "ULTRACEMCO.NS",
    "WIPRO.NS",
]

NEWS_NAME_ALIASES = {
    "RELIANCE": ["Reliance", "RIL", "Mukesh Ambani"],
    "TCS": ["TCS", "Tata Consultancy"],
    "HDFCBANK": ["HDFC Bank"],
    "INFY": ["Infosys", "INFY"],
    "ICICIBANK": ["ICICI Bank"],
    "HINDUNILVR": ["Hindustan Unilever", "HUL"],
    "SBIN": ["State Bank of India", "SBI"],
    "BHARTIARTL": ["Bharti Airtel", "Airtel"],
    "BAJFINANCE": ["Bajaj Finance"],
    "KOTAKBANK": ["Kotak Mahindra Bank", "Kotak Bank"],
    "LT": ["Larsen", "L&T"],
    "TATAMOTORS": ["Tata Motors"],
    "TATASTEEL": ["Tata Steel"],
    "MARUTI": ["Maruti Suzuki", "Maruti"],
    "ASIANPAINTS": ["Asian Paints"],
    "AXISBANK": ["Axis Bank"],
    "WIPRO": ["Wipro"],
    "ADANIENT": ["Adani Enterprises", "Adani"],
    "ADANIPORTS": ["Adani Ports"],
    "HCLTECH": ["HCL Tech", "HCLTech"],
    "SUNPHARMA": ["Sun Pharma"],
    "TITAN": ["Titan Company", "Titan"],
    "NESTLEIND": ["Nestle India", "Nestle"],
    "ONGC": ["ONGC"],
    "NTPC": ["NTPC"],
    "POWERGRID": ["Power Grid"],
    "COALINDIA": ["Coal India"],
    "BPCL": ["BPCL", "Bharat Petroleum"],
    "EICHERMOT": ["Eicher Motors", "Royal Enfield"],
    "HEROMOTOCO": ["Hero MotoCorp", "Hero Motors"],
    "DIVISLAB": ["Divi's Labs", "Divis Lab"],
    "DRREDDY": ["Dr Reddy", "Dr. Reddy"],
    "CIPLA": ["Cipla"],
    "GRASIM": ["Grasim", "Aditya Birla"],
    "JSWSTEEL": ["JSW Steel"],
    "HINDALCO": ["Hindalco"],
    "BRITANNIA": ["Britannia"],
    "APOLLOHOSP": ["Apollo Hospitals"],
    "BAJAJ-AUTO": ["Bajaj Auto"],
    "BAJAJFINSV": ["Bajaj Finserv"],
    "M&M": ["Mahindra"],
    "TATACONSUM": ["Tata Consumer", "Tata Tea"],
    "TECHM": ["Tech Mahindra"],
    "SBILIFE": ["SBI Life"],
    "HDFCLIFE": ["HDFC Life"],
    "TRENT": ["Trent"],
    "ULTRACEMCO": ["UltraTech", "UltraTech Cement"],
}

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    market_close = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return market_open <= now <= market_close


def fetch_recent_candles(symbol):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1d&includePrePost=false"
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    result = data.get("chart", {}).get("result")
    if not result:
        return None
    result = result[0]
    timestamps = result.get("timestamp", [])
    indicators = result.get("indicators", {}).get("quote", [{}])[0]
    if not timestamps or "close" not in indicators:
        return None
    rows = []
    for i, ts in enumerate(timestamps):
        try:
            dt = datetime.fromtimestamp(ts, tz=IST)
            o = indicators.get("open", [None]*len(timestamps))[i]
            h = indicators.get("high", [None]*len(timestamps))[i]
            l = indicators.get("low", [None]*len(timestamps))[i]
            c = indicators.get("close", [None]*len(timestamps))[i]
            v = indicators.get("volume", [None]*len(timestamps))[i]
            if None in (o, h, l, c, v):
                continue
            rows.append({"Datetime": dt, "Open": float(o), "High": float(h), "Low": float(l), "Close": float(c), "Volume": float(v)})
        except (TypeError, ValueError):
            continue
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("Datetime").sort_index()


def load_state():
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            if state.get("date") == today:
                return state
        except Exception:
            pass
    return {"date": today, "alerted": []}


def save_state(state):
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_news_headlines():
    headlines = []
    for feed_url in NEWS_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read().decode(errors="ignore")
            root = ET.fromstring(xml_data)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                if title:
                    headlines.append((title, link))
        except Exception as e:
            print(f"  [!] Failed to fetch news from {feed_url}: {e}")
            continue
    return headlines[:50]


def find_news_for_stock(symbol, headlines):
    clean_symbol = symbol.replace(".NS", "")
    aliases = NEWS_NAME_ALIASES.get(clean_symbol, [clean_symbol])
    matches = []
    for title, link in headlines:
        for alias in aliases:
            if alias.lower() in title.lower():
                matches.append((title, link))
                break
    return matches[:3]


def detect_rally(symbol, headlines):
    df = fetch_recent_candles(symbol)
    if df is None or len(df) < (RALLY_LOOKBACK_CANDLES + 1):
        return None
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    df_today = df[df.index.strftime("%Y-%m-%d") == today_str]
    if len(df_today) < (RALLY_LOOKBACK_CANDLES + 1):
        return None
    current_price = float(df_today.iloc[-1]["Close"])
    price_15m_ago = float(df_today.iloc[-RALLY_LOOKBACK_CANDLES - 1]["Close"])
    move_pct = (current_price - price_15m_ago) / price_15m_ago * 100
    current_vol = float(df_today.iloc[-1]["Volume"])
    vol_baseline = float(df_today.iloc[-VOLUME_BASELINE_CANDLES-1:-1]["Volume"].mean())
    vol_spike = current_vol / vol_baseline if vol_baseline > 0 else 0.0
    if move_pct < RALLY_MOVE_PCT or vol_spike < RALLY_VOLUME_MULT:
        return None
    entry = current_price
    stop_loss = entry * (1 - SL_PCT)
    target = entry * (1 + TARGET_PCT)
    risk = entry - stop_loss
    reward = target - entry
    rr = reward / risk if risk > 0 else 0
    news = find_news_for_stock(symbol, headlines)
    return {
        "symbol": symbol.replace(".NS", ""),
        "current_price": round(current_price, 2),
        "price_15m_ago": round(price_15m_ago, 2),
        "move_pct": round(move_pct, 2),
        "vol_spike": round(vol_spike, 2),
        "entry": round(entry, 2),
        "stop_loss": round(stop_loss, 2),
        "target": round(target, 2),
        "risk_reward": f"1:{round(rr, 1)}",
        "news": news,
        "timestamp": datetime.now(IST).strftime("%H:%M IST"),
    }


def format_alert(alert):
    lines = [
        "RALLY ALERT",
        "=" * 30,
        f"{alert['symbol']}  -  {alert['timestamp']}",
        "",
        f"Price:   INR {alert['current_price']}  ({alert['move_pct']:+.2f}% in 15 min)",
        f"15m ago: INR {alert['price_15m_ago']}",
        f"Volume:  {alert['vol_spike']}x avg",
        "",
        f"Entry:   INR {alert['entry']}",
        f"Stop:    INR {alert['stop_loss']}  (-0.5%)",
        f"Target:  INR {alert['target']}  (+1.5%)",
        f"R:R:     {alert['risk_reward']}",
    ]
    if alert["news"]:
        lines += ["", "Related news:"]
        for i, (title, link) in enumerate(alert["news"], 1):
            lines.append(f"  {i}. {title[:80]}")
    else:
        lines.append("")
        lines.append("(No specific news found - may be technical/sentiment driven)")
    lines.append("")
    lines.append("Educational only. Not investment advice.")
    return "\n".join(lines)


def send_telegram(message):
    if not BOT_TOKEN:
        print("[X] TELEGRAM_BOT_TOKEN not set")
        return False
    if not CHAT_ID:
        print("[X] TELEGRAM_CHAT_ID not set")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": message}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print(f"[OK] Telegram message sent! ID: {result['result']['message_id']}")
                return True
            else:
                print(f"[X] Telegram API error: {result}")
                return False
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else str(e)
        print(f"[X] Telegram HTTP {e.code} error: {error_body}")
        return False
    except Exception as e:
        print(f"[X] Telegram error: {e}")
        return False


def main():
    now_ist = datetime.now(IST)
    print("=" * 60)
    print("  NIFTY 50 RALLY SCANNER")
    print(f"  Run Time (IST) : {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    if not is_market_open():
        print("\n[i] Market is closed (outside 9:15 AM - 3:30 PM IST, or weekend).")
        print("    Skipping this scan.")
        return
    state = load_state()
    alerted_today = set(state["alerted"])
    print(f"\n[i] Already alerted today: {len(alerted_today)} stocks")
    print(f"[i] Scanning {len(NIFTY_50)} stocks for rallies...\n")
    print("[i] Fetching latest news headlines...")
    headlines = fetch_news_headlines()
    print(f"[i] Got {len(headlines)} headlines\n")
    new_alerts = []
    for i, symbol in enumerate(NIFTY_50, 1):
        clean = symbol.replace(".NS", "")
        if clean in alerted_today:
            print(f"  [{i:2d}/{len(NIFTY_50)}] {clean:<14} ... skip (already alerted)")
            continue
        print(f"  [{i:2d}/{len(NIFTY_50)}] {clean:<14} ... ", end="", flush=True)
        alert = detect_rally(symbol, headlines)
        if alert:
            new_alerts.append(alert)
            alerted_today.add(clean)
            print(f"RALLY! {alert['move_pct']:+.2f}% @ {alert['vol_spike']}x vol")
        else:
            print("no rally")
        time.sleep(0.3)
    print(f"\n[i] Found {len(new_alerts)} new rallies")
    if new_alerts:
        print("\n" + "=" * 60)
        print("  SENDING ALERTS")
        print("=" * 60)
        for alert in new_alerts:
            message = format_alert(alert)
            print(f"\n--- Alert for {alert['symbol']} ---")
            print(message)
            send_telegram(message)
            time.sleep(1)
    else:
        print("[i] No new rallies detected. No alerts sent.")
    state["alerted"] = list(alerted_today)
    save_state(state)
    print(f"\n[i] State saved. Total alerted today: {len(alerted_today)}")
    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        error_msg = f"Rally scanner crashed:\n\n{type(e).__name__}: {e}\n\n{traceback.format_exc()[:1500]}"
        print(f"\n[FATAL] {error_msg}")
        try:
            send_telegram(error_msg[:4000])
        except Exception:
            pass
        sys.exit(0)