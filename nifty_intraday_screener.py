#!/usr/bin/env python3
"""
Nifty 50 Intraday Screener
==========================
Screens Nifty 50 stocks for high-probability intraday LONG setups using:
  - VWAP (Volume Weighted Average Price) : price must be above VWAP
  - Volume spike                         : current vol > 1.5x avg of recent candles
  - Positive momentum                    : bullish opening candle OR positive gap

Risk Profile: CONSERVATIVE
  - Stop-loss : 0.5% below entry
  - Target    : 1.5% - 2.0% above entry  (Risk:Reward = 1:3 to 1:4)
  - Max picks : 3 per day

USAGE
-----
  Live scan (during/after market hours, IST):
      python nifty_intraday_screener.py

  Verbose mode (see every stock scanned):
      python nifty_intraday_screener.py --verbose

  Custom output path:
      python nifty_intraday_screener.py --output /path/to/report.json

  Custom number of picks:
      python nifty_intraday_screener.py --max-picks 5

SCHEDULING (run daily at 9:15 AM IST)
-------------------------------------
  Linux/Mac (cron):
      crontab -e
      # Add this line (server must be in IST, or adjust the hour accordingly):
      15 9 * * 1-5 cd /path/to/script && /usr/bin/python3 nifty_intraday_screener.py >> /var/log/nifty_screener.log 2>&1

  Windows (Task Scheduler):
      - Action: Start a program
      - Program: C:\\Python311\\python.exe
      - Arguments: C:\\path\\to\\nifty_intraday_screener.py
      - Start in: C:\\path\\to\\script\\
      - Trigger: Daily at 09:15, weekdays only

FORWARDING TO WHATSAPP / EMAIL
------------------------------
  The script saves a JSON file you can forward:
    - Email   : use Python's smtplib + the JSON file (see README in chat)
    - WhatsApp: use Twilio WhatsApp API (see README in chat)
    - Telegram: use Telegram Bot API  (see README in chat)

DISCLAIMER
----------
This is an EDUCATIONAL tool, NOT investment advice.
Stock trading involves substantial risk of loss.
No analysis can guarantee any % gain. Past performance does not guarantee future results.
Consult a SEBI-registered investment adviser before trading.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Run:  pip install yfinance pandas")
    sys.exit(1)


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

# Nifty 50 stocks (Yahoo Finance tickers with .NS suffix for NSE)
# Update this list periodically as NSE rebalances the index.
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

# ---- Conservative risk rules -------------------------------------------------
SL_PCT             = 0.005   # 0.5% stop-loss
TARGET_PCT_MIN     = 0.015   # 1.5% minimum target
TARGET_PCT_MAX     = 0.020   # 2.0% maximum target
MAX_PICKS_DEFAULT  = 3       # max stocks to recommend per day

# ---- Screener thresholds -----------------------------------------------------
VOLUME_SPIKE_MULT      = 1.5   # current vol must be >= 1.5x average
VOLUME_SPIKE_MULT_OPEN = 2.5   # higher bar for the opening 5-min candle (it's naturally high)
VWAP_MIN_DISTANCE      = 0.001 # price must be at least 0.1% above VWAP
MIN_CANDLES_TODAY      = 1     # min candles today needed to evaluate


# ----------------------------------------------------------------------------
# Indicator calculations
# ----------------------------------------------------------------------------

def calc_vwap(df: pd.DataFrame) -> float:
    """Calculate VWAP from intraday OHLCV data.
    VWAP = sum(typical_price * volume) / sum(volume)
    typical_price = (high + low + close) / 3
    """
    if df.empty or df['Volume'].sum() == 0:
        return float('nan')
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3.0
    return float((typical_price * df['Volume']).sum() / df['Volume'].sum())


def calc_volume_avg(df: pd.DataFrame, window: int = 20) -> float:
    """Average volume over the last N candles."""
    if df.empty:
        return 0.0
    if len(df) < window:
        return float(df['Volume'].mean())
    return float(df['Volume'].tail(window).mean())


# ----------------------------------------------------------------------------
# Stock screener
# ----------------------------------------------------------------------------

def screen_stock(symbol: str, verbose: bool = False, demo_mode: bool = False) -> dict | None:
    """
    Screen one stock for an intraday long setup.
    Returns dict if a setup is found, else None.

    If demo_mode=True, treat the most recent trading day available in the data
    as 'today' (useful for testing on weekends / outside market hours).
    """
    try:
        ticker = yf.Ticker(symbol)

        # Pull 5 days of 5-min candles (gives us previous-day volume baseline + today)
        try:
            hist = ticker.history(period="5d", interval="5m")
        except Exception as e:
            if verbose:
                print(f"fetch error: {e}")
            return None
        if hist.empty:
            if verbose:
                print("no data returned (symbol may be delisted on Yahoo)")
            return None

        # Convert index to IST and split today vs prior days
        hist.index = hist.index.tz_convert(IST)
        all_dates = hist.index.strftime("%Y-%m-%d").unique().tolist()

        if not all_dates:
            return None

        if demo_mode:
            # Use the most recent trading day in the data as 'today'
            target_date = all_dates[-1]
        else:
            target_date = datetime.now(IST).strftime("%Y-%m-%d")

        hist_today = hist[hist.index.strftime("%Y-%m-%d") == target_date]
        hist_prior = hist[hist.index.strftime("%Y-%m-%d") != target_date]

        if len(hist_today) < MIN_CANDLES_TODAY:
            if verbose:
                print(f"not enough candles on {target_date} ({len(hist_today)})")
            return None

        # Previous close (for gap calculation)
        daily = ticker.history(period="5d", interval="1d")
        if daily.empty or len(daily) < 1:
            return None
        prev_close = float(daily['Close'].iloc[-2]) if len(daily) >= 2 else float(daily['Close'].iloc[0])

        # Current state
        last_candle   = hist_today.iloc[-1]
        current_price = float(last_candle['Close'])
        current_vol   = float(last_candle['Volume'])

        # VWAP from today's session only
        vwap = calc_vwap(hist_today)
        if vwap != vwap:  # NaN check
            return None

        # Volume baseline: prior days' last 20 candles (more stable than including today's open burst)
        vol_baseline = calc_volume_avg(hist_prior.tail(20)) if not hist_prior.empty else calc_volume_avg(hist)

        # Decide volume-spike threshold based on whether this is the opening candle
        is_opening_candle = len(hist_today) <= 1
        vol_threshold_mult = VOLUME_SPIKE_MULT_OPEN if is_opening_candle else VOLUME_SPIKE_MULT

        vol_spike = current_vol / vol_baseline if vol_baseline > 0 else 0.0
        volume_ok = vol_spike >= vol_threshold_mult

        # Momentum checks
        gap_pct        = (current_price - prev_close) / prev_close * 100
        bullish_candle = float(last_candle['Close']) > float(last_candle['Open'])
        above_vwap     = current_price > vwap * (1 + VWAP_MIN_DISTANCE)
        positive_mom   = (gap_pct > 0) or bullish_candle

        if verbose:
            print(f"     date={target_date} price={current_price:.2f} vwap={vwap:.2f} "
                  f"gap={gap_pct:+.2f}% vol_spike={vol_spike:.2f}x "
                  f"(need {vol_threshold_mult}x) "
                  f"above_vwap={above_vwap} bull_candle={bullish_candle}")

        if not (above_vwap and volume_ok and positive_mom):
            return None

        # Build trade plan
        entry      = current_price
        stop_loss  = entry * (1 - SL_PCT)
        target_low = entry * (1 + TARGET_PCT_MIN)
        target_high= entry * (1 + TARGET_PCT_MAX)

        risk       = entry - stop_loss
        reward_lo  = target_low  - entry
        reward_hi  = target_high - entry
        rr_lo      = reward_lo / risk if risk > 0 else 0
        rr_hi      = reward_hi / risk if risk > 0 else 0

        # Composite score (higher = better)
        #   - volume spike weight: 40%
        #   - gap strength weight : 30%
        #   - vwap distance weight: 30%
        score = (
            vol_spike                       * 0.4 +
            max(gap_pct, 0)                 * 0.3 +
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
            "reason":        (f"Price above VWAP, volume {vol_spike:.1f}x avg "
                              f"(threshold {vol_threshold_mult}x), gap {gap_pct:+.2f}%"),
        }

    except Exception as e:
        print(f"  [!] Error scanning {symbol}: {e}", file=sys.stderr)
        return None


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nifty 50 intraday screener (VWAP + Volume, Conservative risk)"
    )
    parser.add_argument("--output", "-o", default=None,
                        help="Path to save JSON report (default: ./nifty_picks_<timestamp>.json)")
    parser.add_argument("--max-picks", type=int, default=MAX_PICKS_DEFAULT,
                        help=f"Max stocks to recommend (default: {MAX_PICKS_DEFAULT})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-stock scan details")
    parser.add_argument("--demo", action="store_true",
                        help="Demo mode: use most recent trading day as 'today' "
                             "(for testing on weekends / outside market hours)")
    args = parser.parse_args()

    now_ist = datetime.now(IST)

    print("=" * 72)
    print("  NIFTY 50 INTRADAY SCREENER")
    print(f"  Run Time (IST) : {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode           : {'DEMO (uses last trading day)' if args.demo else 'LIVE'}")
    print("  Strategy       : VWAP + Volume Spike (LONG setups only)")
    print("  Risk Profile   : CONSERVATIVE  (SL 0.5%, Target 1.5-2%, RR 1:3-1:4)")
    print(f"  Universe       : Nifty 50 ({len(NIFTY_50)} stocks)")
    print("=" * 72)
    print()

    if now_ist.weekday() >= 5:
        print("  NOTE: Today is a weekend. Markets are closed.")
        print("        Script will still scan using last available data.")
        print()

    # Check market hours (9:15 - 15:30 IST)
    market_open  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    if not (market_open <= now_ist <= market_close) and now_ist.weekday() < 5:
        print("  NOTE: Outside live market hours (9:15 AM - 3:30 PM IST).")
        print("        Using most recent available data (may be from previous close).")
        print()

    results = []
    print(f"Scanning {len(NIFTY_50)} Nifty 50 stocks...\n")

    for i, symbol in enumerate(NIFTY_50, 1):
        if args.verbose:
            print(f"  [{i:2d}/{len(NIFTY_50)}] {symbol.replace('.NS',''):<14} ", end="")
        result = screen_stock(symbol, verbose=args.verbose, demo_mode=args.demo)
        if result:
            results.append(result)
            if args.verbose:
                print("=> SETUP FOUND")
        else:
            if args.verbose:
                print("=> skip")
        # Be nice to Yahoo Finance rate limits
        time.sleep(0.25)

    # Rank by composite score, take top N
    results.sort(key=lambda x: x['score'], reverse=True)
    top_picks = results[: args.max_picks]

    # Console report
    print()
    print("=" * 72)
    print(f"  TOP {len(top_picks)} INTRADAY PICKS  -  {now_ist.strftime('%d %b %Y')}")
    print("=" * 72)
    print()

    if not top_picks:
        print("  No high-probability setups found in this scan.")
        print()
        print("  This is NORMAL. Better to skip trading than force a bad trade.")
        print()
        print("  Likely causes:")
        print("    - Market opened flat or weak")
        print("    - Low volume across the board")
        print("    - No clear VWAP breakouts yet")
        print()
        print("  Tip: Re-run at 9:45 AM or 10:15 AM IST for more setups")
        print("       (by then 6-12 five-min candles have formed, signals are stronger)")
    else:
        for i, pick in enumerate(top_picks, 1):
            print(f"  #{i}  {pick['symbol']}")
            print(f"       Entry         : INR {pick['entry']}")
            print(f"       Stop Loss     : INR {pick['stop_loss']}   (-0.5%)")
            print(f"       Target Range  : INR {pick['target_low']} - {pick['target_high']}   (+1.5% to +2%)")
            print(f"       Risk : Reward : {pick['risk_reward']}")
            print(f"       VWAP          : INR {pick['vwap']}")
            print(f"       Gap vs prev   : {pick['gap_pct']:+.2f}%")
            print(f"       Volume Spike  : {pick['volume_spike']}x  avg")
            print(f"       Why picked    : {pick['reason']}")
            print()

    print("=" * 72)
    print("  DISCLAIMER : Educational tool, NOT investment advice.")
    print("               Trading involves substantial risk of loss.")
    print("               Consult a SEBI-registered advisor before trading.")
    print("=" * 72)

    # JSON report
    report = {
        "run_time_ist":   now_ist.isoformat(),
        "mode":           "demo" if args.demo else "live",
        "strategy":       "VWAP + Volume Spike (LONG setups)",
        "risk_profile":   "conservative",
        "risk_rules": {
            "stop_loss_pct":      SL_PCT,
            "target_min_pct":     TARGET_PCT_MIN,
            "target_max_pct":     TARGET_PCT_MAX,
            "max_picks":          args.max_picks,
            "volume_spike_mult":  VOLUME_SPIKE_MULT,
        },
        "universe":            "Nifty 50",
        "total_scanned":       len(NIFTY_50),
        "setups_found":        len(results),
        "picks":               top_picks,
        "disclaimer":          "Educational tool, NOT investment advice. Trading involves risk.",
    }

    output_path = Path(args.output) if args.output else (
        Path(__file__).parent / f"nifty_picks_{now_ist.strftime('%Y%m%d_%H%M')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n  JSON report saved to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
