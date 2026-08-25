#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Indian Daily Stock Scanner  (educational tool - NOT investment advice)

Scans a watchlist of liquid NSE stocks every day and produces a ranked
shortlist for short-term trading (intraday-to-swing, 1 day to ~1 week):

  * Market regime check   -> should you even be buying today?
  * Per-stock technicals  -> RSI, MACD, EMA/SMA trends, ATR volatility, volume
  * Score & rank          -> trend + momentum + volume + volatility + setup
  * Trade plan            -> entry, stop-loss, targets, position size for a
                             user-defined capital and risk % per trade
  * Avoid list            -> stocks showing distribution / breakdown signals

USAGE
  python stock_scanner.py                      # default: capital Rs 1,00,000, 1.5% risk
  python stock_scanner.py --capital 200000     # your total trading capital
  python stock_scanner.py --risk 1.0           # risk % per trade (1-2 recommended)
  python stock_scanner.py --refresh            # ignore cached data, re-download
  python stock_scanner.py --limit 20           # quick test on first N symbols

Output:
  - console summary
  - reports/picks_YYYY-MM-DD.html   (standalone report - open in any browser)
  - reports/picks_YYYY-MM-DD.json   (machine readable copy)

DISCLAIMER
  This software is for education and research only. It is not SEBI-registered
  investment advice. Markets can and do move against you; never risk money
  you cannot afford to lose, and use stop-losses on every trade.
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

from watchlist import (WATCHLIST, MARKET_INDEX, MARKET_INDEX_NAME,
                       MARKET_VIX)

try:
    from prob_model import success_probability
except Exception:  # noqa: BLE001
    success_probability = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "reports")
CACHE_DIR = os.path.join(HERE, "cache")
CACHE_EXT = ".pkl"

# --- Pick history (cooldown memory) ------------------------------------
# Persisted across runs so the scanner does not re-pick a stock it picked
# recently. On GitHub Actions the workflow commits this file back to the
# repo each day; locally it just lives next to the script.
HISTORY_FILE = os.path.join(HERE, "picks_history.json")
DEFAULT_COOLDOWN = 7          # sessions a stock is 'on cooldown' after being picked
HISTORY_KEEP = 90             # max entries kept in the history file


def load_pick_history():
    """Return list of {'date','ticker'} most-recent-first, or []."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, encoding="utf-8") as fh:
                h = json.load(fh)
            if isinstance(h, list):
                return [e for e in h if isinstance(e, dict) and e.get("ticker")]
    except Exception:  # noqa: BLE001
        pass
    return []


def save_pick_history(entries):
    """Save pick history (chronological, oldest first)."""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(entries[-HISTORY_KEEP:], fh, indent=1)
    except Exception:  # noqa: BLE001
        pass

DEFAULT_CAPITAL = 1_00_000          # Rs - change with --capital
DEFAULT_RISK_PCT = 1.5              # % of capital risked per trade
MAX_RSI_PENALTY = 78.0              # RSI above this = overbought / chase risk
MIN_BARS = 120                      # need at least this many daily bars
MIN_AVG_SHARES = 300_000            # 20-day avg volume filter (shares)
MIN_AVG_VALUE_CR = 10               # 20-day avg traded value filter (Rs crore)
ATR_MIN_PCT, ATR_MAX_PCT = 1.0, 9.0 # volatility band for tradable names

# Score weights (total 100 before penalties)
W = {
    "trend": 35,
    "momentum": 25,
    "volume": 15,
    "volatility": 10,
    "setup": 15,
}

MAX_WORKERS = 10


def ist_now():
    return dt.datetime.now(IST)


# ----------------------------------------------------------------------------
# Technical indicators
# ----------------------------------------------------------------------------
def sma(s, n):
    return s.rolling(n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    ru = up.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rd = down.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = ru / rd.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()


def macd(close, fast=12, slow=26, signal=9):
    macd_line = ema(close, fast) - ema(close, slow)
    sig = ema(macd_line, signal)
    return macd_line, sig, macd_line - sig


def inr(x):
    """Format a number with Indian digit grouping, e.g. 1234567 -> 12,34,567"""
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "-"
    neg = x < 0
    s = str(int(round(abs(x))))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return ("-" if neg else "") + s


# ----------------------------------------------------------------------------
# Data fetching (with cache)
# ----------------------------------------------------------------------------
def fetch_one(sym, period="1y", attempts=3):
    last_err = None
    for i in range(attempts):
        try:
            df = yf.Ticker(sym).history(period=period, auto_adjust=True)
            if df is not None and not df.empty:
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
                if len(df) > 0:
                    return df
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.4 * (i + 1))
    if last_err is not None:
        print(f"  ! failed {sym}: {last_err}")
    return None


def fetch_all(refresh=False, limit=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    symbols = WATCHLIST if limit is None else WATCHLIST[:limit]
    tickers = [t for t, _ in symbols]
    date_key = ist_now().strftime("%Y-%m-%d")
    cache_path = os.path.join(CACHE_DIR, f"data_{date_key}{CACHE_EXT}")

    market_open = _market_is_open()
    if not refresh and os.path.exists(cache_path) and not market_open:
        try:
            with open(cache_path, "rb") as fh:
                data = pickle.load(fh)
            if data:  # never trust an empty cache
                print(f"[cache] loaded {len(data)} tickers from {os.path.basename(cache_path)}")
                return data
        except Exception:  # noqa: BLE001
            pass

    data = {}
    done = 0
    print(f"[fetch] downloading {len(tickers)} symbols ...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_one, t): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            df = fut.result()
            if df is not None:
                data[t] = df
            done += 1
            if done % 20 == 0 or done == len(tickers):
                print(f"  ... {done}/{len(tickers)} ({time.time()-t0:.0f}s)")
    print(f"[fetch] got {len(data)}/{len(tickers)} symbols in {time.time()-t0:.0f}s")

    # cache only when market closed (final data) and we actually have data
    if not market_open and data:
        try:
            with open(cache_path, "wb") as fh:
                pickle.dump(data, fh)
        except Exception:  # noqa: BLE001
            pass
    return data


def _market_is_open():
    now = ist_now()
    if now.weekday() >= 5:  # Sat / Sun
        return False
    secs = now.hour * 3600 + now.minute * 60 + now.second
    return 9 * 3600 + 15 * 60 <= secs <= 15 * 3600 + 30 * 60


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------
def _col(df, name):
    """Return a Series for a column, tolerating MultiIndex columns."""
    if name in df.columns:
        return df[name]
    if isinstance(df.columns, pd.MultiIndex):
        return df[name][df[name].columns[0]] if name in df.columns.get_level_values(0) else None
    return None


def analyze(ticker, name, df, nifty_df):
    """Compute stats + score for one stock. Returns dict or None (filtered out)."""
    try:
        close = df["Close"].astype(float)
        if len(close) < MIN_BARS or close.iloc[-1] <= 0:
            return None

        s20, s50 = sma(close, 20), sma(close, 50)
        e9, e21 = ema(close, 9), ema(close, 21)
        r14 = rsi(close)
        # weekly trend filter: RSI-14 computed on Friday-ending weekly closes
        # (measured: adds a small real gain on the 2-yr backtest, T1 37.9%->38.6%,
        #  avg +0.210R->+0.229R, PF 1.47->1.52)
        try:
            wk = close.resample("W-FRI").last().dropna()
            weekly_rsi_val = float(rsi(wk).iloc[-1]) if len(wk) >= 16 else None
        except Exception:  # noqa: BLE001
            weekly_rsi_val = None
        macd_l, macd_s, macd_h = macd(close)
        at = atr(df)
        vol = df["Volume"].astype(float)

        last = close.iloc[-1]
        prev = close.iloc[-2]
        chg_pct = (last / prev - 1) * 100
        atr_val = at.iloc[-1]
        atr_pct = atr_val / last * 100 if atr_val > 0 and last > 0 else 0.0

        # liquidity
        avg_shares = vol.tail(20).mean()
        avg_value_cr = (close.tail(20) * vol.tail(20)).mean() / 1e7
        if avg_shares < MIN_AVG_SHARES or avg_value_cr < MIN_AVG_VALUE_CR:
            return None
        if not (ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT):
            return None

        # 52-week range
        h52 = close.max()
        l52 = close.min()
        dist_52h = (last / h52 - 1) * 100
        dist_52l = (last / l52 - 1) * 100

        # 20-day high and proximity
        high20 = close.tail(20).max()
        dist_20h = (last / high20 - 1) * 100

        # returns
        r5 = (last / close.iloc[-6] - 1) * 100 if len(close) > 6 else 0
        r20 = (last / close.iloc[-21] - 1) * 100 if len(close) > 21 else 0
        r60 = (last / close.iloc[-61] - 1) * 100 if len(close) > 61 else 0

        # volume ratios
        v5 = vol.tail(5).mean()
        v20 = vol.tail(20).mean()
        v10 = vol.tail(10).mean()
        v30 = vol.tail(30).mean()
        vol_ratio_5_20 = v5 / v20 if v20 > 0 else 1
        vol_accel = v10 / v30 if v30 > 0 else 1
        vol_ratio_today = vol.iloc[-1] / v20 if v20 > 0 else 1

        # today's candle
        o = df["Open"].iloc[-1]
        body = last - o
        red_big = body / atr_val < -1.2 if atr_val > 0 else False
        drop_big = chg_pct < -2.0 and vol_ratio_today > 1.5

        # gap up
        gap_pct = (o / prev - 1) * 100 if prev > 0 else 0

        extended = (last - s20.iloc[-1]) / atr_val if atr_val > 0 else 0

        # ---------------- scoring ----------------
        sc = {"trend": 0.0, "momentum": 0.0, "volume": 0.0, "volatility": 0.0, "setup": 0.0}
        reasons = []
        s50v = s50.iloc[-1]
        s50_ok = not math.isnan(s50v)

        # trend (35)
        if last > e21.iloc[-1]:
            sc["trend"] += 10
        if s50_ok and e21.iloc[-1] > s50v:
            sc["trend"] += 8
        if s50_ok and last > s50v:
            sc["trend"] += 8
        if s50_ok and s20.iloc[-1] > s50v:
            sc["trend"] += 5
        if r20 > 0:
            sc["trend"] += 4
        if sc["trend"] >= 26:
            reasons.append("strong uptrend")

        # momentum (25)
        rv = r14.iloc[-1]
        if 52 <= rv <= 68:
            sc["momentum"] += 12
        elif 50 <= rv < 52 or 68 < rv <= 74:
            sc["momentum"] += 7
        elif 45 <= rv < 50 or 74 < rv <= 78:
            sc["momentum"] += 3
        elif rv < 40 or rv > 78:
            sc["momentum"] -= 6
        if macd_h.iloc[-1] > 0:
            sc["momentum"] += 6
        if macd_l.iloc[-1] > macd_s.iloc[-1]:
            sc["momentum"] += 3
        if 0.5 <= r5 <= 6:
            sc["momentum"] += 4
        if 52 <= rv <= 68 and macd_h.iloc[-1] > 0:
            reasons.append("momentum healthy")

        # volume (15)
        if vol_ratio_5_20 >= 1.15:
            sc["volume"] += 6
        elif vol_ratio_5_20 >= 1.0:
            sc["volume"] += 4
        else:
            sc["volume"] += 1
        if vol_accel >= 1.05:
            sc["volume"] += 5
        else:
            sc["volume"] += 1
        if vol_ratio_today >= 1.3:
            sc["volume"] += 4
        elif vol_ratio_today >= 1.0:
            sc["volume"] += 2
        if sc["volume"] >= 11:
            reasons.append("volume supporting")

        # volatility fit (10)
        if 1.5 <= atr_pct <= 4.5:
            sc["volatility"] += 10
        elif 1.2 <= atr_pct < 1.5 or 4.5 < atr_pct <= 6:
            sc["volatility"] += 6
        elif 1.0 <= atr_pct < 1.2 or 6 < atr_pct <= 8:
            sc["volatility"] += 3

        # setup (15)
        if -6 <= dist_52h <= 0:
            sc["setup"] += 9
        elif -12 <= dist_52h < -6:
            sc["setup"] += 6
        elif -20 <= dist_52h < -12:
            sc["setup"] += 3
        if dist_20h >= -2.5:
            sc["setup"] += 6
        if sc["setup"] >= 12:
            reasons.append("near highs / breakout zone")

        total = sum(sc.values())

        # penalties
        if rv > MAX_RSI_PENALTY:
            total -= 12
        if extended > 2.5:
            total -= 10
        if red_big:
            total -= 14
            reasons.append("big red candle")
        if drop_big:
            total -= 8
        if gap_pct > 3:
            total -= 4
        if s50_ok and last <= s50v:
            total -= 6

        # recent swing low (last 5 completed sessions, excluding today's live bar)
        recent_low = float(df["Low"].iloc[-6:-1].min()) if len(df) >= 6 else last - 3 * atr_val

        return {
            "ticker": ticker, "name": name, "close": last, "prev": prev,
            "chg_pct": chg_pct, "rsi": rv, "atr": atr_val, "atr_pct": atr_pct,
            "avg_shares": avg_shares, "avg_value_cr": avg_value_cr,
            "dist_52h": dist_52h, "dist_52l": dist_52l,
            "r5": r5, "r20": r20, "r60": r60,
            "macd_h": macd_h.iloc[-1], "s20": s20.iloc[-1], "s50": s50v,
            "e21": e21.iloc[-1], "vol_ratio_5_20": vol_ratio_5_20,
            "vol_ratio_today": vol_ratio_today, "red_big": red_big,
            "drop_big": drop_big, "extended": extended,
            "recent_low": recent_low,
            "gap_pct": gap_pct,
            "weekly_rsi": weekly_rsi_val,
            "scores": sc, "total": total, "reasons": reasons,
            "date": str(df.index[-1].date()),
        }
    except Exception:  # noqa: BLE001
        return None


def vix_context(vix_df):
    """India VIX level -> plain-language stress gauge."""
    if vix_df is None or len(vix_df) == 0:
        return {"vix": None, "vix_label": "n/a", "vix_note": ""}
    v = float(vix_df["Close"].iloc[-1])
    if v < 15:
        label, note = "Calm", "VIX is low - markets complacent. Still keep stops."
    elif v <= 20:
        label, note = "Normal", "VIX in normal range - conditions fine but stay disciplined."
    elif v <= 25:
        label, note = "Elevated", "VIX elevated - expect bigger swings. Size down and tighten stops."
    else:
        label, note = "High fear", "VIX very high - markets stressed. Best to avoid fresh longs or stay in cash."
    return {"vix": v, "vix_label": label, "vix_note": note}


def market_regime(nifty_df, vix_df=None):
    if nifty_df is None or len(nifty_df) < 200:
        out = {"label": "Unknown", "color": "gray",
               "note": "Not enough index data to judge the market."}
        out.update(vix_context(vix_df))
        return out
    c = nifty_df["Close"].astype(float)
    s50, s200 = sma(c, 50), sma(c, 200)
    last, r14 = c.iloc[-1], rsi(c).iloc[-1]
    r20 = (last / c.iloc[-21] - 1) * 100
    above200 = last > s200.iloc[-1]
    above50 = last > s50.iloc[-1]
    bullish = above200 and above50 and r14 >= 50
    bearish = (not above200) and r14 < 45
    vix = vix_context(vix_df)
    if bullish:
        out = {"label": "Bullish", "color": "#16a34a",
               "note": "Index above 50 & 200-day averages with positive RSI. Favour buying dips in strong names.",
               "r14": r14, "r20": r20, "above200": True, "above50": above50}
    elif bearish:
        out = {"label": "Bearish - caution", "color": "#dc2626",
               "note": "Index below its 200-day average. Prefer staying in cash or very small, quick trades with tight stops.",
               "r14": r14, "r20": r20, "above200": False, "above50": above50}
    else:
        out = {"label": "Neutral / mixed", "color": "#d97706",
               "note": "Index mixed vs its averages. Trade only the highest-scoring picks with strict stop-losses.",
               "r14": r14, "r20": r20, "above200": above200, "above50": above50}
    out.update(vix)
    return out


# ----------------------------------------------------------------------------
# Trade plan
# ----------------------------------------------------------------------------
def trade_plan(stat, capital, risk_pct):
    """Entry / stop / targets / position size."""
    entry = stat["close"]
    # stop = max(1.2 x ATR below entry, recent 5-day swing low)  -> tightest sensible
    sl = max(entry - 1.2 * stat["atr"], stat["recent_low"])
    if sl >= entry:  # structure is above price; fall back to ATR stop
        sl = entry - 1.2 * stat["atr"]
    risk_per_share = entry - sl
    if risk_per_share <= 0 or entry <= 0:
        return None
    risk_rs = capital * risk_pct / 100.0
    qty = int(risk_rs // risk_per_share)
    if qty <= 0:
        return None
    notional = entry * qty
    if notional > capital * 0.95:  # can't afford
        qty = int((capital * 0.95) // entry)
        notional = entry * qty
    t1 = entry + 1.5 * risk_per_share
    t2 = entry + 2.6 * risk_per_share
    return {
        "entry": entry,
        "sl": sl,
        "sl_pct": (entry - sl) / entry * 100,
        "t1": t1, "t2": t2,
        "t1_pct": (t1 / entry - 1) * 100,
        "t2_pct": (t2 / entry - 1) * 100,
        "qty": qty,
        "notional": notional,
        "notional_pct": notional / capital * 100,
        "risk_rs": risk_rs,
    }


# ----------------------------------------------------------------------------
# HTML report
# ----------------------------------------------------------------------------
CSS = """
:root{--green:#16a34a;--red:#dc2626;--amber:#d97706;--ink:#0f172a;--mut:#64748b;--bg:#f1f5f9;--card:#ffffff;--line:#e2e8f0}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);padding:24px;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:24px}
h2{font-size:17px;margin:0 0 10px}
.sub{color:var(--mut);font-size:13px;margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:16px 0;box-shadow:0 1px 3px rgba(15,23,42,.06)}
.banner{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.chip{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:#f8fafc;color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.l,th.l{text-align:left}
tr:hover td{background:#f8fafc}
.pos{color:var(--green)} .neg{color:var(--red)} .mut{color:var(--mut)}
.big{font-size:34px;font-weight:800}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-size:14px;margin-top:6px}
.kv b{text-align:right;color:var(--mut);font-weight:500}
.pick-box{border-left:5px solid var(--green);padding-left:16px;margin:10px 0}
.why{font-size:13.5px;color:#334155;margin-top:10px}
.avoid{color:var(--red)}
.note{font-size:12.5px;color:var(--mut)}
.foot{font-size:11.5px;color:var(--mut);border-top:1px solid var(--line);padding-top:12px;margin-top:20px}
.live{background:#fef3c7;color:#92400e;border:1px solid #fcd34d;border-radius:10px;padding:10px 14px;font-size:13px;margin:14px 0}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:6px;background:#eef2ff;color:#3730a3;margin:2px 2px 0 0}
"""


def _tag(s):
    return f'<span class="tag">{html.escape(s)}</span>'


def sparkline(closes, color):
    """Inline SVG 30-day line for the top pick."""
    if len(closes) < 5:
        return ""
    xs = closes[-30:]
    w, h = 320, 70
    lo, hi = min(xs), max(xs)
    rng = (hi - lo) or 1
    pts = []
    n = len(xs)
    for i, v in enumerate(xs):
        x = i * (w / max(n - 1, 1))
        y = h - 6 - (v - lo) / rng * (h - 12)
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'style="display:block;margin-top:8px" preserveAspectRatio="none">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round"/></svg>')


def build_html(cfg, meta, regime, picks, avoids, failed, filter_info=None):
    date_str = meta["date"]
    live_note = ""
    if meta["market_open"]:
        live_note = ("<div class='live'>⚠ Market is OPEN right now — today's candle is "
                     "still forming and all prices/levels are LIVE. Run this again after "
                     "3:30 PM IST (or before 9:15 AM tomorrow) for the finalized daily plan.</div>")

    regime_chip = f'<span class="chip" style="background:{regime["color"]}">{html.escape(regime["label"])}</span>'
    r14_txt = f'{regime["r14"]:.0f}' if "r14" in regime else "-"
    r20_txt = f'{regime["r20"]:+.1f}%' if "r20" in regime else "-"
    above_txt = "Yes" if regime.get("above200") else "No"
    vix_txt = f'{regime["vix"]:.1f}' if regime.get("vix") else "-"
    vix_lbl = f'({regime["vix_label"]})' if regime.get("vix_label") else ""
    vix_note = regime.get("vix_note", "")
    regime_html = f"""
    <div class="card">
      <div class="banner"><h2>Market regime — {html.escape(MARKET_INDEX_NAME)}</h2>{regime_chip}</div>
      <p style="margin-top:8px">{html.escape(regime["note"])}</p>
      <p class="note" style="margin-top:6px">NIFTY RSI(14): {r14_txt} &nbsp;·&nbsp;
      20-day change: {r20_txt} &nbsp;·&nbsp;
      Above 200-DMA: {above_txt} &nbsp;·&nbsp;
      India VIX: {vix_txt} {vix_lbl} &nbsp;·&nbsp; {html.escape(vix_note)}</p>
    </div>"""

    # ---------------- pick of the day ----------------
    prob_chip = ""
    if picks and picks[0].get("prob") is not None:
        prob_chip = (f'<span class="chip" style="background:#475569" '
                     f'title="Historical estimate from 2-yr backtest of similar setups">'
                     f'T1 prob ~{picks[0]["prob"]*100:.0f}%</span>')
    filter_banner = ""
    if filter_info and filter_info.get("enabled") and picks:
        _pr = picks[0].get("prob", 0)
        wk_txt = ""
        if filter_info.get("weekly_enabled") and picks[0].get("weekly_rsi") is not None:
            wk_txt = (f' · weekly RSI {picks[0]["weekly_rsi"]:.1f} ≥ '
                      f'{filter_info["weekly_min"]:.0f}')
        filter_banner = (f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;color:#15803d;'
                         f'border-radius:8px;padding:8px 12px;font-size:13px;margin-top:10px">'
                         f'✅ Passed improved filter: probability {_pr*100:.1f}% ≥ '
                         f'{filter_info["min_prob"]:.0%} · RSI {picks[0]["rsi"]:.1f} in '
                         f'{filter_info["rsi_min"]:.0f}–{filter_info["rsi_max"]:.0f} band{wk_txt} · '
                         f'<b>{filter_info["passed_count"]} of {filter_info["base_count"]}</b> '
                         f'candidates passed today.'
                         + (f'<br>🚫 Cooldown: <b>{filter_info["cooldown_skipped"]}</b> higher-scoring '
                            f'candidate(s) skipped — picked in the last '
                            f'{filter_info["cooldown"]} sessions (re-picks lose money on average).'
                            if filter_info.get("cooldown_skipped") else "")
                         + '</div>')

    pick_html = ""
    if picks:
        p = picks[0]
        tp = p.get("plan")
        color = "var(--green)"
        sub = f"{p['name']} ({p['ticker'].replace('.NS','')})"
        sl_pct = tp["sl_pct"] if tp else 0
        risk_line = f"Risk per trade: Rs {inr(cfg['risk_rs'])} ({cfg['risk_pct']}% of Rs {inr(cfg['capital'])})" if tp else ""
        whys = " · ".join(p["reasons"]) if p["reasons"] else "trend & setup"
        chase_warn = ""
        if p.get("gap_pct", 0) > 3 or p.get("chg_pct", 0) > 2.5:
            chase_warn = ("<p class='note' style='color:#b45309'>⚠ " + html.escape(sub) +
                          f" already moved +{max(p.get('gap_pct', 0), p.get('chg_pct', 0)):.1f}% at/after "
                          "open — DO NOT chase. Wait for a dip toward the entry zone or skip today.</p>")
        pick_html = f"""
    <div class="card" style="border:2px solid {color};border-radius:16px">
      <div class="banner">
        <div>
          <h2 style="margin:0">🏆 Pick of the day</h2>
          <div class="sub">{html.escape(sub)} · scanned {date_str} {meta['time']} IST</div>
        </div>
        <span class="chip" style="background:{color}">Score {p['total']:.0f}/100</span>
        {prob_chip}
      </div>
      {filter_banner}
      <div class="grid2" style="margin-top:12px">
        <div class="pick-box">
          <div class="big">₹{p['close']:,.2f}</div>
          <div class="{('pos' if p['chg_pct']>=0 else 'neg')}">Last close {p['chg_pct']:+.2f}% today</div>
          <div class="kv" style="margin-top:10px">
            <b>Entry zone</b><span>₹{tp['entry']:,.2f} (market / near close)</span>
            <b>Stop-loss</b><span style="color:var(--red)">₹{tp['sl']:,.2f} ({tp['sl_pct']:.1f}%)</span>
            <b>Target 1</b><span style="color:var(--green)">₹{tp['t1']:,.2f} (+{tp['t1_pct']:.1f}%)</span>
            <b>Target 2</b><span style="color:var(--green)">₹{tp['t2']:,.2f} (+{tp['t2_pct']:.1f}%)</span>
            <b>Quantity</b><span>{inr(tp['qty'])} shares</span>
            <b>Capital used</b><span>Rs {inr(tp['notional'])} ({tp['notional_pct']:.0f}% of capital)</span>
            <b>Risk on trade</b><span>Rs {inr(tp['risk_rs'])}</span>
          </div>
          <p class="why">Why: {html.escape(whys)}. RSI {p['rsi']:.0f}, ATR {p['atr_pct']:.1f}%,
             {p['dist_52h']:+.1f}% from 52-week high, 5-day avg volume {p['vol_ratio_5_20']:.2f}× 20-day.</p>
          <p class="note">{html.escape(risk_line)} — never risk more than this.</p>
          {"<p class='note' style='color:#b45309'>⚠ This single position uses more than 40% of your capital. Beginners are safer splitting into 2–3 smaller positions.</p>" if tp and tp['notional_pct'] > 40 else ""}
          {chase_warn}
        </div>
        <div>
          <p class="note" style="margin-bottom:4px">Last 30 sessions</p>
          {sparkline(p.get("_closes", []), "#16a34a")}
        </div>
      </div>
    </div>"""

    # ---------------- top list ----------------
    rows = ""
    for i, p in enumerate(picks, 1):
        tp = p["plan"] or {}
        rows += f"""<tr>
        <td>{i}</td><td class="l"><b>{p['ticker'].replace('.NS','')}</b><div class="mut" style="font-size:11px">{html.escape(p['name'])}</div></td>
        <td>₹{p['close']:,.2f}</td>
        <td class="{('pos' if p['chg_pct']>=0 else 'neg')}">{p['chg_pct']:+.2f}%</td>
        <td>{p['rsi']:.0f}</td>
        <td>{p['atr_pct']:.1f}%</td>
        <td>₹{p['avg_value_cr']:,.0f} cr</td>
        <td>{p['dist_52h']:+.1f}%</td>
        <td>{('~%.0f%%' % (p.get('prob',0)*100)) if p.get('prob') is not None else '-'}</td>
        <td><b>{p['total']:.0f}</b></td>
        <td class="l" style="white-space:normal;min-width:190px">
          ₹{tp.get('entry', p['close']):,.2f} / ₹{tp.get('sl',0):,.2f} / ₹{tp.get('t1',0):,.2f} / ₹{tp.get('t2',0):,.2f}</td>
        </tr>"""

    top_html = f"""
    <div class="card">
      <h2>Top candidates — swing (1 day to ~1 week)</h2>
      <div style="overflow-x:auto">
      <table>
        <thead><tr class="l"><th>#</th><th class="l">Stock</th><th>LTP</th><th>Day%</th><th>RSI</th>
        <th>ATR%</th><th>Avg Val</th><th>vs 52wH</th><th>Prob</th><th>Score</th><th class="l">Entry / SL / T1 / T2</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>
      <p class="note" style="margin-top:8px">ATR% = average daily move. vs 52wH = % below 52-week high.
         Scores blend trend, momentum, volume, volatility and breakout proximity (max 100).</p>
    </div>"""

    # ---------------- avoid list ----------------
    avoid_rows = ""
    for a in avoids[:10]:
        reason = "big red candle / distribution" if (a.get("red_big") or a.get("drop_big")) else "downtrend (below 50-DMA, weak RSI)"
        avoid_rows += (f'<tr><td class="l">{a["ticker"].replace(".NS","")}</td>'
                       f'<td class="l">{html.escape(a["name"])}</td>'
                       f'<td class="neg">{a["chg_pct"]:+.2f}%</td>'
                       f'<td>{a["rsi"]:.0f}</td>'
                       f'<td class="l mut">{html.escape(reason)}</td></tr>')
    avoid_html = f"""
    <div class="card">
      <h2 style="color:var(--red)">⚠ Avoid today</h2>
      <div style="overflow-x:auto">
      <table>
        <thead><tr class="l"><th class="l">Stock</th><th class="l">Name</th><th>Day%</th><th>RSI</th><th class="l">Why</th></tr></thead>
        <tbody>{avoid_rows}</tbody>
      </table>
      </div>
    </div>"""

    # ---------------- how to use ----------------
    howto = """
    <div class="card">
      <h2>How to use this (30-second checklist)</h2>
      <ol style="margin-left:18px;font-size:14px">
        <li><b>Check the regime chip first.</b> Bearish → skip trading or size way down.</li>
        <li>Trade only the <b>top 1–3</b> picks. Don't trade all of them.</li>
        <li>Enter <b>at/near the open or on a dip toward the entry zone</b>; never chase a +3% gap.</li>
        <li>Place the <b>stop-loss immediately</b> at entry. If it hits, you lose ~1.5% of capital — that's the plan working.</li>
        <li>Book part at <b>Target 1</b>, trail the rest toward <b>Target 2</b>.</li>
        <li>Hold max <b>1 day to ~1 week</b>. If nothing happens in 5 sessions, exit — don't convert a swing into a long-term bet.</li>
        <li>Keep <b>2–3 positions max</b> so one bad day can't hurt you.</li>
      </ol>
    </div>"""

    failed_html = ""
    if failed:
        failed_html = (f'<p class="note">Skipped (data unavailable): {html.escape(", ".join(failed))}</p>')

    html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Stock Picks — {date_str}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
  <div class="banner">
    <h1>📈 Indian Daily Stock Scanner</h1>
    <span class="chip" style="background:#0f172a">{date_str} · {meta['time']} IST</span>
  </div>
  <div class="sub">Scan of {meta['scanned']} liquid NSE stocks · capital ₹{inr(cfg['capital'])} · risk {cfg['risk_pct']}% per trade · holding 1–7 days</div>
  {live_note}
  {regime_html}
  {pick_html}
  {top_html}
  {avoid_html}
  {howto}
  {failed_html}
  <div class="foot">
    <b>Important:</b> This report is generated by software for <b>education and research only</b>. It is not SEBI-registered
    investment advice. Past patterns never guarantee future results; markets can move against you quickly.
    Always use stop-losses, risk only what you can afford to lose (your plan: ≤ {cfg['risk_pct']}% of capital per trade),
    and consider consulting a SEBI-registered advisor before trading.
    Data source: Yahoo Finance (delayed/live quotes, may contain errors). Generated at {meta['time']} IST on {date_str}.
  </div>
</div></body></html>"""
    return html_doc


# ----------------------------------------------------------------------------
# Console output
# ----------------------------------------------------------------------------
def print_console(meta, regime, picks, avoids, failed, cfg, filter_info=None):
    print("\n" + "=" * 78)
    print(f"  INDIAN DAILY STOCK SCANNER — {meta['date']} {meta['time']} IST")
    print(f"  Capital ₹{inr(cfg['capital'])} · Risk {cfg['risk_pct']}% per trade · Holding 1–7 days")
    print("=" * 78)

    if meta["market_open"]:
        print("\n⚠ MARKET IS OPEN — prices below are LIVE. Run after 3:30 PM for final levels.")

    print(f"\nMARKET REGIME [{regime['label']}]: {regime['note']}")
    if regime.get("vix"):
        print(f"  India VIX: {regime['vix']:.1f} ({regime.get('vix_label', '')}) — {regime.get('vix_note', '')}")

    if not picks:
        print("\n⚠ No stock passed the quality filters today. Best action: STAY IN CASH.")
        if filter_info and filter_info.get("enabled"):
            print(f"   (Improved filter: {filter_info['base_count']} candidates were "
                  f"checked, {filter_info['passed_count']} passed prob≥{filter_info['min_prob']:.0%} "
                  f"& RSI {filter_info['rsi_min']:.0f}-{filter_info['rsi_max']:.0f}.)")
        print("   That is a valid trade decision — preserving capital is priority #1.")
        return

    p = picks[0]
    tp = p["plan"]
    print("\n" + "-" * 78)
    print(f"  🏆 PICK OF THE DAY : {p['ticker'].replace('.NS','')}  ({p['name']})   Score {p['total']:.0f}/100")
    print("-" * 78)
    if filter_info and filter_info.get("enabled"):
        print(f"  ✅ Improved filter: prob {p.get('prob', 0)*100:.1f}% ≥ {filter_info['min_prob']:.0%} · "
              f"RSI {p['rsi']:.1f} in {filter_info['rsi_min']:.0f}-{filter_info['rsi_max']:.0f}"
              + (f" · weekly RSI {p.get('weekly_rsi', 0):.1f} ≥ {filter_info['weekly_min']:.0f}"
                 if filter_info.get('weekly_enabled') else "") + f" · "
              f"({filter_info['passed_count']}/{filter_info['base_count']} candidates passed)")
        if filter_info.get("cooldown_skipped"):
            print(f"  🚫 Cooldown      : {filter_info['cooldown_skipped']} higher-scoring candidate(s) "
                  f"skipped (picked in the last {filter_info['cooldown']} sessions)")
    print(f"  Last close      : ₹{p['close']:,.2f}  ({p['chg_pct']:+.2f}% today)")
    print(f"  Entry zone      : ₹{tp['entry']:,.2f}")
    print(f"  Stop-loss       : ₹{tp['sl']:,.2f}  (-{tp['sl_pct']:.1f}%)")
    print(f"  Target 1        : ₹{tp['t1']:,.2f}  (+{tp['t1_pct']:.1f}%)")
    print(f"  Target 2        : ₹{tp['t2']:,.2f}  (+{tp['t2_pct']:.1f}%)")
    print(f"  Quantity        : {inr(tp['qty'])} shares (uses ₹{inr(tp['notional'])}, {tp['notional_pct']:.0f}% of capital)")
    print(f"  Risk if SL hit  : ₹{inr(tp['risk_rs'])} = {cfg['risk_pct']}% of capital")
    if tp["notional_pct"] > 40:
        print("  ⚠ Note          : position uses >40% of capital — consider 2-3 smaller positions instead")
    if p.get("gap_pct", 0) > 3 or p.get("chg_pct", 0) > 2.5:
        print(f"  ⚠ CHASE GUARD    : {p['ticker'].replace('.NS','')} already moved "
              f"+{max(p.get('gap_pct', 0), p.get('chg_pct', 0)):.1f}% — DO NOT chase. "
              "Wait for a dip toward entry or skip.")
    if p.get("prob") is not None:
        print(f"  Success prob    : ~{p['prob']*100:.0f}% chance of hitting Target 1 "
              f"(historical estimate, 2-yr backtest)")
    print(f"  Why             : {(' · '.join(p['reasons']) if p['reasons'] else 'trend + setup')} | RSI {p['rsi']:.0f} | ATR {p['atr_pct']:.1f}%")

    print("\n" + "-" * 78)
    print("  TOP CANDIDATES")
    print("-" * 78)
    hdr = f"  {'#':<3}{'Stock':<14}{'LTP':>10}{'Day%':>8}{'RSI':>6}{'ATR%':>7}{'Score':>7}"
    print(hdr)
    for i, q in enumerate(picks[:10], 1):
        print(f"  {i:<3}{q['ticker'].replace('.NS',''):<14}{q['close']:>10,.2f}"
              f"{q['chg_pct']:>+8.2f}{q['rsi']:>6.0f}{q['atr_pct']:>7.1f}{q['total']:>7.0f}")

    if avoids:
        print("\n  AVOID TODAY (distribution / weak):", ", ".join(
            a["ticker"].replace(".NS", "") for a in avoids[:8]))
    if failed:
        print(f"\n  Skipped (no data): {', '.join(failed[:10])}{' …' if len(failed) > 10 else ''}")
    print("\n" + "=" * 78)
    print("  Educational tool — not investment advice. Use stop-losses.")
    print("=" * 78 + "\n")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Indian daily stock scanner (educational)")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help="total trading capital in Rs")
    ap.add_argument("--risk", type=float, default=DEFAULT_RISK_PCT,
                    help="risk % of capital per trade (1–2 recommended)")
    ap.add_argument("--refresh", action="store_true", help="re-download data (ignore cache)")
    ap.add_argument("--limit", type=int, default=None, help="scan only first N symbols (testing)")
    ap.add_argument("--no-html", action="store_true", help="skip HTML report generation")
    ap.add_argument("--min-prob", type=float, default=0.28,
                    help="minimum success probability (0-1) for a pick to qualify (default 0.28)")
    ap.add_argument("--rsi-min", type=float, default=50.0, help="RSI lower bound of the sweet-spot band")
    ap.add_argument("--rsi-max", type=float, default=65.0, help="RSI upper bound of the sweet-spot band")
    ap.add_argument("--weekly-rsi-min", type=float, default=50.0,
                    help="min weekly RSI(14) trend filter (default 50; measured gain)")
    ap.add_argument("--no-weekly-filter", action="store_true",
                    help="disable the weekly RSI trend filter")
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN,
                    help="sessions a stock is skipped after being picked (default 7; measured: "
                         "re-picks <=10d avg -0.068R vs first-time +0.278R)")
    ap.add_argument("--no-cooldown", action="store_true",
                    help="disable the re-pick cooldown (not recommended)")
    args = ap.parse_args()

    if args.risk <= 0 or args.risk > 5:
        sys.exit("Risk % must be between 0 and 5 (1–2 is the sane range for a beginner).")
    if args.capital <= 0:
        sys.exit("Capital must be positive.")

    data = fetch_all(refresh=args.refresh, limit=args.limit)
    if not data:
        sys.exit("No data could be downloaded. Check your internet connection and try again.")

    nifty_df = data.get(MARKET_INDEX) or fetch_one(MARKET_INDEX)
    vix_df = data.get(MARKET_VIX) or fetch_one(MARKET_VIX)
    regime = market_regime(nifty_df, vix_df)

    stats = []
    for ticker, name in (WATCHLIST if args.limit is None else WATCHLIST[:args.limit]):
        df = data.get(ticker)
        if df is None or len(df) < MIN_BARS:
            continue
        st = analyze(ticker, name, df, nifty_df)
        if st is not None:
            st["_closes"] = df["Close"].astype(float).tolist()
            st["plan"] = trade_plan(st, args.capital, args.risk)
            if st["plan"] is None:
                continue
            if success_probability is not None:
                st["prob"] = success_probability(
                    st["total"], st["rsi"], st["dist_52h"],
                    st["vol_ratio_5_20"], st["atr_pct"],
                    regime.get("above50", False))
            else:
                st["prob"] = None
            stats.append(st)

    stats.sort(key=lambda s: s["total"], reverse=True)
    base_picks = [s for s in stats if s["total"] >= 40 and not (s["red_big"] or s["drop_big"])]
    # --- IMPROVED FILTER: probability + RSI sweet spot + weekly RSI trend ---
    # (each filter was measured on the 2-yr backtest; weekly RSI>=50 added a real
    #  gain while 200-DMA/combo filters HURT and were deliberately NOT added)
    use_weekly = not args.no_weekly_filter
    if success_probability is not None:
        picks = [s for s in base_picks
                 if s.get("prob", 0) >= args.min_prob
                 and args.rsi_min <= s["rsi"] <= args.rsi_max
                 and (not use_weekly
                      or s.get("weekly_rsi") is None  # no weekly data -> pass leniently
                      or s.get("weekly_rsi", 0) >= args.weekly_rsi_min)]
    else:
        picks = base_picks

    # --- COOLDOWN: don't re-pick a stock picked recently (measured big win:
    # re-picks <=10 sessions avg -0.068R vs first-time +0.278R; cooldown 7
    # lifts PF 1.46 -> 1.55) ---
    use_cooldown = not args.no_cooldown and args.cooldown > 0
    history = load_pick_history() if use_cooldown else []
    recent_tickers = {}  # ticker -> last pick date (most recent entries first)
    for e in history[:args.cooldown if args.cooldown > 0 else 0]:
        t = e.get("ticker")
        if t and t not in recent_tickers:
            recent_tickers[t] = e.get("date", "")
    cooldown_skipped = 0
    if use_cooldown and recent_tickers:
        kept = []
        for s in picks:
            if s["ticker"] in recent_tickers:
                cooldown_skipped += 1
                continue
            kept.append(s)
        picks = kept
    picks = picks[:10]
    filter_info = {
        "enabled": success_probability is not None,
        "min_prob": args.min_prob, "rsi_min": args.rsi_min, "rsi_max": args.rsi_max,
        "weekly_enabled": use_weekly,
        "weekly_min": args.weekly_rsi_min,
        "cooldown_enabled": use_cooldown,
        "cooldown": args.cooldown if use_cooldown else 0,
        "cooldown_skipped": cooldown_skipped,
        "base_count": len(base_picks), "passed_count": len(picks),
    }
    pick_tickers = {p["ticker"] for p in picks}
    avoid_candidates = [
        s for s in stats
        if s["ticker"] not in pick_tickers
        and ((s["rsi"] < 45 and s["close"] < s["s50"]) or s["red_big"] or s["drop_big"])
    ]
    avoids, seen = [], set()
    for s in sorted(avoid_candidates, key=lambda s: s["chg_pct"]):
        if s["ticker"] not in seen:
            seen.add(s["ticker"])
            avoids.append(s)
        if len(avoids) >= 10:
            break

    scanned = len(data)
    failed = [t for t, _ in (WATCHLIST if args.limit is None else WATCHLIST[:args.limit])
              if t not in data]

    now = ist_now()
    meta = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "market_open": _market_is_open(),
        "scanned": scanned,
    }
    cfg = {"capital": args.capital, "risk_pct": args.risk, "risk_rs": args.capital * args.risk / 100}

    # record today's pick in the cooldown history (always, even with --no-html)
    if use_cooldown and picks:
        _history = load_pick_history()
        _today_str = meta["date"]
        if not (_history and _history[-1].get("date") == _today_str):
            _history.append({"date": _today_str, "ticker": picks[0]["ticker"]})
            save_pick_history(_history)

    print_console(meta, regime, picks, avoids, failed, cfg, filter_info)

    if not args.no_html:
        os.makedirs(REPORT_DIR, exist_ok=True)
        html_path = os.path.join(REPORT_DIR, f"picks_{meta['date']}.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(build_html(cfg, meta, regime, picks, avoids, failed, filter_info))
        json_path = os.path.join(REPORT_DIR, f"picks_{meta['date']}.json")
        slim = []
        for s in picks:
            slim.append({k: v for k, v in s.items()
                         if k not in ("_closes", "scores", "prev", "s20", "s50", "e21",
                                      "atr", "extended", "avg_shares", "macd_h", "vol_ratio_today",
                                      "recent_low", "dist_52l", "r60", "r5", "drop_big", "red_big")})
        # keep the full trade plan in the JSON (used by daily_email.py)
        for s, sl in zip(picks, slim):
            if s.get("plan"):
                sl["plan"] = s["plan"]
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump({"meta": meta, "filter": filter_info, "regime": regime["label"],
                       "regime_note": regime.get("note", ""),
                       "vix": regime.get("vix"),
                       "vix_label": regime.get("vix_label"),
                       "above50": regime.get("above50", False),
                       "capital": args.capital, "risk_pct": args.risk, "picks": slim},
                      fh, indent=2, default=str)
        print(f"Report saved: {html_path}")


if __name__ == "__main__":
    main()
