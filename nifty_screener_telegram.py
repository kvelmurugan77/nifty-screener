#!/usr/bin/env python3
"""
Nifty 50 Intraday Screener + Telegram Sender  (PythonAnywhere Compatible)
=========================================================================
Self-contained: scans Nifty 50, picks top 3 intraday long setups,
sends them to your Telegram.

THIS VERSION IS OPTIMIZED FOR PYTHONANYWHERE FREE TIER:
  - Uses query2.finance.yahoo.com (whitelisted on PythonAnywhere)
  - Bypasses yfinance library (which uses blocked query1 endpoint)
  - Direct urllib requests only - no external libs except pandas
  - No API key needed, no rate limits

USAGE
-----
  Test run (uses last trading day's data):
      python nifty_screener_telegram.py

PYTHONANYWHERE SCHEDULE
-----------------------
  Schedule this script daily at 03:45 UTC = 09:15 AM IST
  via PythonAnywhere -> Tasks tab.

CONFIGURATION
-------------
  Edit BOT_TOKEN and CHAT_ID below (or use env vars).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("Installing pandas...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "pandas"])
    import pandas as pd


# ============================================================================
# CONFIGURATION - Edit these values
# ============================================================================

# Your Telegram bot credentials (from @BotFather)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "PASTE_YOUR_CHAT_ID_HERE")

# Set LIVE_MODE=1 to use today's date instead of last trading day
LIVE_MODE = os.environ.get("LIVE_MODE", "0") == "1"

# ============================================================================
# STRATEGY CONFIGURATION
# ============================================================================

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

IST = timezone(timedelta(hours=5, minutes=30))

SL_PCT             = 0.005
TARGET_PCT_MIN     = 0.015
TARGET_PCT_MAX     = 0.020
MAX_PICKS          = 3

VOLUME_SPIKE_MULT      = 1.5
VOLUME_SPIKE_MULT_OPEN = 2.5
VWAP_MIN_DISTANCE      = 0.001


# ============================================================================
# Yahoo Finance data fetcher (uses query2 - whitelisted on PythonAnywhere)
# ============================================================================

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_yahoo_chart(symbol, interval="5m", range_="5d"):
    """Fetch intraday chart data from Yahoo Finance query2 endpoint.
    Returns pandas DataFrame with columns: Open, High, Low, Close, Volume
    Indexed by tz-aware Datetime (IST). Returns None on error."""
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval={interval}&range={range_}&includePrePost=false")
    try:
        req = urllib.request.Request(url, headers=YAHOO_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return None

    if data.get("chart", {}).get("error"):
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
            o = indicators.get("open",   [None]*len(timestamps))[i]
            h = indicators.get("high",   [None]*len(timestamps))[i]
            l = indicators.get("low",    [None]*len(timestamps))[i]
            c = indicators.get("close",  [None]*len(timestamps))[i]
            v = indicators.get("volume", [None]*len(timestamps))[i]
            if None in (o, h, l, c, v):
                continue
            rows.append({
                "Datetime": dt,
                "Open":   float(o),
                "High":   float(h),
                "Low":    float(l),
                "Close":  float(c),
                "Volume": float(v),
            })
        except (TypeError, ValueError):
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows).set_index("Datetime").sort_index()
    return df


# ============================================================================
# Indicator calculations
# ============================================================================

def calc_vwap(df):
    if df.empty or df['Volume'].sum() == 0:
        return float('nan')
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3.0
    return float((typical_price * df['Volume']).sum() / df['Volume'].sum())


def calc_volume_avg(df, window=20):
    if df.empty:
        return 0.0
    if len(df) < window:
        return float(df['Volume'].mean())
    return float(df['Volume'].tail(window).mean())


# ============================================================================
# Stock screener
# ============================================================================

def screen_stock(symbol, verbose=False):
    try:
        hist = fetch_yahoo_chart(symbol, interval="5m", range_="5d")
        if hist is None or hist.empty:
            return None

        all_dates = hist.index.strftime("%Y-%m-%d").unique().tolist()
        if not all_dates:
            return None

        if LIVE_MODE:
            target_date = datetime.now(IST).strftime("%Y-%m-%d")
            if target_date not in all_dates:
                target_date = all_dates[-1]
        else:
            target_date = all_dates[-1]  # Demo: use most recent trading day

        hist_today = hist[hist.index.strftime("%Y-%m-%d") == target_date]
        hist_prior = hist[hist.index.strftime("%Y-%m-%d") != target_date]

        if len(hist_today) < 1:
            return None

        # Get previous close from prior days' daily aggregates
        if hist_prior.empty:
            return None
        prev_close = float(hist_prior['Close'].resample('1D').last().dropna().iloc[-1])

        last_candle   = hist_today.iloc[-1]
        current_price = float(last_candle['Close'])
        current_vol   = float(last_candle['Volume'])

        vwap = calc_vwap(hist_today)
        if vwap != vwap:
            return None

        vol_baseline = calc_volume_avg(hist_prior.tail(20))

        is_opening_candle = len(hist_today) <= 1
        vol_threshold_mult = VOLUME_SPIKE_MULT_OPEN if is_opening_candle else VOLUME_SPIKE_MULT

        vol_spike = current_vol / vol_baseline if vol_baseline > 0 else 0.0
        volume_ok = vol_spike >= vol_threshold_mult

        gap_pct        = (current_price - prev_close) / prev_close * 100
        bullish_candle = float(last_candle['Close']) > float(last_candle['Open'])
        above_vwap     = current_price > vwap * (1 + VWAP_MIN_DISTANCE)
        positive_mom   = (gap_pct > 0) or bullish_candle

        if not (above_vwap and volume_ok and positive_mom):
            return None

        entry       = current_price
        stop_loss   = entry * (1 - SL_PCT)
        target_low  = entry * (1 + TARGET_PCT_MIN)
        target_high = entry * (1 + TARGET_PCT_MAX)

        risk       = entry - stop_loss
        reward_lo  = target_low  - entry
        reward_hi  = target_high - entry
        rr_lo      = reward_lo / risk if risk > 0 else 0
        rr_hi      = reward_hi / risk if risk > 0 else 0

        score = (
            vol_spike * 0.4 +
            max(gap_pct, 0) * 0.3 +
            ((current_price - vwap) / vwap * 100) * 0.3
        )

        return {
            "symbol":        symbol.replace(".NS", ""),
            "current_price": round(current_price, 2),
            "vwap":          round(vwap, 2),
            "prev_close":    round(prev_close, 2),
            "gap_pct":       round(gap_pct, 2),
            "volume_spike":  round(vol_spike, 2),
            "entry":         round(entry, 2),
            "stop_loss":     round(stop_loss, 2),
            "target_low":    round(target_low, 2),
            "target_high":   round(target_high, 2),
            "risk_reward":   f"1:{round(rr_lo, 1)} to 1:{round(rr_hi, 1)}",
            "score":         round(score, 3),
            "reason":        f"Price above VWAP, volume {vol_spike:.1f}x avg, threshold {vol_threshold_mult}x, gap {gap_pct:+.2f}%",
        }
    except Exception as e:
        print(f"  [!] Error scanning {symbol}: {e}", file=sys.stderr)
        return None


# ============================================================================
# Telegram sender
# ============================================================================

def format_message(report):
    mode_label = "LIVE" if LIVE_MODE else "DEMO (last trading day)"
    lines = [
        "NIFTY 50 INTRADAY PICKS",
        "=" * 40,
        f"Run: {report['run_time_ist'][:19]} IST",
        f"Mode: {mode_label}",
        f"Strategy: {report['strategy']}",
        f"Setups found: {report['setups_found']}/{report['total_scanned']}",
        "",
    ]
    if not report['picks']:
        lines += [
            "No high-probability setups today.",
            "Better to skip than force a bad trade.",
            "",
            "Likely causes:",
            "  - Market opened flat or weak",
            "  - Low volume across the board",
            "  - No clear VWAP breakouts yet",
        ]
    else:
        for i, p in enumerate(report['picks'], 1):
            lines += [
                f"#{i}  {p['symbol']}",
                f"  Entry: INR {p['entry']}  |  SL: INR {p['stop_loss']} (-0.5%)",
                f"  Target: INR {p['target_low']} - INR {p['target_high']} (+1.5% to +2%)",
                f"  R:R {p['risk_reward']}  |  VWAP INR {p['vwap']}",
                f"  Gap {p['gap_pct']:+.2f}%  |  Vol {p['volume_spike']}x avg",
                f"  Why: {p['reason']}",
                "",
            ]
    lines.append("Educational only. Not investment advice.")
    return "\n".join(lines)


def send_telegram(message):
    if "PASTE_YOUR" in BOT_TOKEN or "PASTE_YOUR" in CHAT_ID:
        print("\n[X] Telegram credentials not configured!")
        print("    Edit BOT_TOKEN and CHAT_ID at the top of this file, OR")
        print("    set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text":    message,
        # Plain text mode - avoids all Markdown/HTML parsing errors
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print(f"[OK] Telegram message sent! Message ID: {result['result']['message_id']}")
                return True
            else:
                print(f"[X] Telegram API error: {result}")
                return False
    except urllib.error.HTTPError as e:
        # Read the actual error body from Telegram - this tells us EXACTLY what's wrong
        error_body = e.read().decode() if hasattr(e, 'read') else str(e)
        print(f"[X] Telegram HTTP {e.code} error:")
        print(f"    Response body: {error_body}")
        return False
    except Exception as e:
        print(f"[X] Telegram error: {e}")
        return False


# ============================================================================
# Main
# ============================================================================

def main():
    now_ist = datetime.now(IST)

    print("=" * 60)
    print("  NIFTY 50 INTRADAY SCREENER + TELEGRAM")
    print(f"  Run Time (IST) : {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode           : {'LIVE' if LIVE_MODE else 'DEMO (last trading day)'}")
    print("  Strategy       : VWAP + Volume Spike (LONG setups)")
    print("  Risk Profile   : CONSERVATIVE (SL 0.5%, Target 1.5-2%)")
    print(f"  Universe       : Nifty 50 ({len(NIFTY_50)} stocks)")
    print("=" * 60)

    results = []
    print(f"\nScanning {len(NIFTY_50)} Nifty 50 stocks...\n")

    for i, symbol in enumerate(NIFTY_50, 1):
        print(f"  [{i:2d}/{len(NIFTY_50)}] {symbol.replace('.NS',''):<14} ... ", end="", flush=True)
        result = screen_stock(symbol)
        if result:
            results.append(result)
            print(f"SETUP FOUND (score={result['score']})")
        else:
            print("skip")
        time.sleep(0.4)  # Be polite to Yahoo

    results.sort(key=lambda x: x['score'], reverse=True)
    top_picks = results[: MAX_PICKS]

    report = {
        "run_time_ist":   now_ist.isoformat(),
        "mode":           "live" if LIVE_MODE else "demo",
        "strategy":       "VWAP + Volume Spike (LONG setups)",
        "risk_profile":   "conservative",
        "universe":       "Nifty 50",
        "total_scanned":  len(NIFTY_50),
        "setups_found":   len(results),
        "picks":          top_picks,
        "disclaimer":     "Educational tool, NOT investment advice.",
    }

    print("\n" + "=" * 60)
    print(f"  TOP {len(top_picks)} PICKS")
    print("=" * 60)
    if top_picks:
        for i, p in enumerate(top_picks, 1):
            print(f"\n  #{i} {p['symbol']}")
            print(f"      Entry: INR {p['entry']} | SL: INR {p['stop_loss']} | Target: INR {p['target_low']}-{p['target_high']}")
    else:
        print("\n  No setups found today. (This is normal.)")

    print("\n" + "=" * 60)
    print("  SENDING TO TELEGRAM")
    print("=" * 60)
    message = format_message(report)
    print("\nMessage preview:")
    print(message)
    print()
    send_telegram(message)

    out_path = Path.home() / "nifty_picks_latest.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nJSON saved to: {out_path}")
    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Even if main crashes, try to send an error notification to Telegram
        # so the user knows something went wrong (instead of silent failure)
        import traceback
        error_msg = f"Nifty screener crashed:\n\n{type(e).__name__}: {e}\n\nFull traceback:\n{traceback.format_exc()[:1500]}"
        print(f"\n[FATAL] {error_msg}")
        try:
            send_telegram(error_msg[:4000])  # Telegram limit is 4096 chars
        except Exception:
            pass  # If even error reporting fails, just exit
        # Exit 0 so GitHub Actions doesn't mark it as failed
        # (We've already notified the user via Telegram if possible)
        sys.exit(0)
