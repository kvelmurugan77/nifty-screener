#!/usr/bin/env python3
"""
Nifty 50 Morning Briefing - Daily Pre-Market Report
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
MIN_GAP_PCT = 1.0
MOMENTUM_MOVE_PCT = 2.0
MOMENTUM_VOL_MULT = 1.5

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

NEWS_FEEDS = [
    "https://www.moneycontrol.com/rss/latestnews.xml",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
]

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


def fetch_daily_history(symbol):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
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
            dt = datetime.fromtimestamp(ts, tz=IST).date()
            o = indicators.get("open", [None]*len(timestamps))[i]
            h = indicators.get("high", [None]*len(timestamps))[i]
            l = indicators.get("low", [None]*len(timestamps))[i]
            c = indicators.get("close", [None]*len(timestamps))[i]
            v = indicators.get("volume", [None]*len(timestamps))[i]
            if None in (o, h, l, c, v):
                continue
            rows.append({"Date": dt, "Open": float(o), "High": float(h), "Low": float(l), "Close": float(c), "Volume": float(v)})
        except (TypeError, ValueError):
            continue
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("Date").sort_index()


def fetch_news_headlines(limit=8):
    headlines = []
    for feed_url in NEWS_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read().decode(errors="ignore")
            root = ET.fromstring(xml_data)
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                if title:
                    headlines.append(title)
        except Exception:
            continue
    return headlines[:limit]


def analyze_stock(symbol):
    df = fetch_daily_history(symbol)
    if df is None or len(df) < 2:
        return None
    last_day = df.iloc[-1]
    prev_day = df.iloc[-2]
    yday_close = float(prev_day["Close"])
    today_close = float(last_day["Close"])
    yday_move = (today_close - yday_close) / yday_close * 100
    if len(df) >= 3:
        prev_prev_vol = float(df.iloc[-3]["Volume"])
        last_vol = float(last_day["Volume"])
        vol_spike = last_vol / prev_prev_vol if prev_prev_vol > 0 else 1.0
    else:
        vol_spike = 1.0
    today_open = float(last_day["Open"])
    gap_pct = (today_open - yday_close) / yday_close * 100
    return {
        "symbol": symbol.replace(".NS", ""),
        "today_open": round(today_open, 2),
        "yday_close": round(yday_close, 2),
        "gap_pct": round(gap_pct, 2),
        "yday_move": round(yday_move, 2),
        "vol_spike": round(vol_spike, 2),
        "today_close": round(today_close, 2),
    }


def get_top_candidates():
    all_stocks = []
    for i, symbol in enumerate(NIFTY_50, 1):
        clean = symbol.replace(".NS", "")
        print(f"  [{i:2d}/{len(NIFTY_50)}] {clean:<14} ... ", end="", flush=True)
        result = analyze_stock(symbol)
        if result:
            all_stocks.append(result)
            print(f"gap={result['gap_pct']:+.2f}% yday={result['yday_move']:+.2f}%")
        else:
            print("skip")
        time.sleep(0.3)
    gap_candidates = sorted([s for s in all_stocks if s["gap_pct"] >= MIN_GAP_PCT], key=lambda x: x["gap_pct"], reverse=True)[:3]
    momentum_candidates = sorted([s for s in all_stocks if s["yday_move"] >= MOMENTUM_MOVE_PCT and s["vol_spike"] >= MOMENTUM_VOL_MULT], key=lambda x: x["yday_move"], reverse=True)[:3]
    return gap_candidates, momentum_candidates


def format_briefing(gap_candidates, momentum_candidates, headlines):
    now_str = datetime.now(IST).strftime("%A, %d %b %Y")
    lines = [f"MORNING BRIEFING - {now_str}", "=" * 40, ""]
    if gap_candidates:
        lines.append("GAP-UP CANDIDATES (watch for opening range breakout):")
        for i, s in enumerate(gap_candidates, 1):
            lines += [f"  #{i} {s['symbol']}  -  gap +{s['gap_pct']}%", f"      Open: INR {s['today_open']} (prev close INR {s['yday_close']})"]
        lines.append("")
    else:
        lines.append("No significant gap-ups today.")
        lines.append("")
    if momentum_candidates:
        lines.append("YESTERDAY'S MOMENTUM (continued strength expected):")
        for i, s in enumerate(momentum_candidates, 1):
            lines += [f"  #{i} {s['symbol']}  -  {s['yday_move']:+.2f}% with {s['vol_spike']}x volume", f"      Close: INR {s['today_close']}"]
        lines.append("")
    else:
        lines.append("No strong momentum from yesterday.")
        lines.append("")
    if headlines:
        lines.append("TOP NEWS HEADLINES:")
        for i, h in enumerate(headlines, 1):
            lines.append(f"  {i}. {h[:80]}")
        lines.append("")
    lines.append("=" * 40)
    lines.append("Strategy: Wait for 15-min candle to confirm direction.")
    lines.append("Use 0.5% stop-loss, 1.5% target (R:R 1:3).")
    lines.append("Watch for rally alerts throughout the day.")
    lines.append("")
    lines.append("Educational only. Not investment advice.")
    return "\n".join(lines)


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("[X] Telegram credentials not configured")
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
    print("  NIFTY 50 MORNING BRIEFING")
    print(f"  Run Time (IST) : {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    if now_ist.weekday() >= 5:
        print("\n[i] Weekend - markets closed. Skipping briefing.")
        return
    print("\n[i] Scanning Nifty 50 for gap-ups and momentum...\n")
    gap_candidates, momentum_candidates = get_top_candidates()
    print("\n[i] Fetching latest news headlines...")
    headlines = fetch_news_headlines(limit=8)
    print(f"[i] Got {len(headlines)} headlines")
    print("\n[i] Sending briefing to Telegram...")
    message = format_briefing(gap_candidates, momentum_candidates, headlines)
    print("\n" + "=" * 60)
    print("BRIEFING PREVIEW:")
    print("=" * 60)
    print(message)
    print("=" * 60)
    send_telegram(message)
    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        error_msg = f"Morning briefing crashed:\n\n{type(e).__name__}: {e}\n\n{traceback.format_exc()[:1500]}"
        print(f"\n[FATAL] {error_msg}")
        try:
            send_telegram(error_msg[:4000])
        except Exception:
            pass
        sys.exit(0)