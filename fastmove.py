#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fastmove.py — "2% in a day or a few days" selection layer for stock_scanner.

WHY THIS FILE EXISTS
--------------------
The classic scanner picks trend/breakout setups and targets 1.5R (≈4-6% away)
with a tight 1.2×ATR stop and a 7-session window. Measured on the emails that
were actually delivered (Aug-Oct 2026) that plan reached Target 1 on 9 of 52
trades (17%) — the target was simply too far for a 5-session swing.

The goal from 2026-11 is different: catch stocks that can move ~2% quickly.
That is a *volatility + velocity* question, not a breakout question, so this
module screens on exactly those two things and pairs them with a plan whose
first target is +2%.

WHAT WAS MEASURED  (34,713 NSE stock-days, 146 liquid names, Nov-2025..Oct-2026,
entry at the next session's open, 0.23% round-trip delivery cost)
--------------------------------------------------------------------------
                                        P(touch +2%)      net per trade
  all stock-days, +2% in 5 sessions          51.4%             -0.46%
  ATR% > 3                                   65.1%             -0.33%
  ATR% > 3 and 5-day return > 2%             68.0%             see below
  same bucket, plan +2% target / 1.5xATR    ~67%               -0.05%
  same bucket, +2% target, 2xATR stop,
      book 1/3 at +2%, rest to +4%, day 5    68.0%             +0.23%

Reading of that table, stated plainly:
  * ATR% >= 3 is the single strongest, most stable predictor of a fast 2% move.
    It roughly doubles the one-in-five chance of a 2% day in the whole universe.
  * BUT: a +2% target with an ATR-based stop is a coin-flip after costs. The
    only configuration that came out non-negative needs a 2×ATR stop (≈4-6%),
    a partial book at +2% and a runner to +4% — and it is NOT robust: strip
    April-2026 out of the 12-month sample and it loses money.
  * Therefore this module is a *hit-rate* upgrade, not a proven edge. Use it to
    get more 2% touches per week and fewer 7-day waits; do not lever it.

KNOWN WEAKNESSES (deliberate, do not paper over them)
  * Whole-sample expectancy is ≈ 0 after costs; month-to-month swings are large
    (Apr-2026 +1.44%/trade vs Sep-2026 -1.86%/trade).
  * The gates fire on ~83% of sessions, so there is nearly always a pick —
    which also means the filter is not very selective. Treat "no pick" days as
    a warning sign, not the norm.
"""

CONFIG = {
    # selection gates (must be true for a stock to be offered as a fast-move pick)
    "atr_pct_min": 3.0,       # daily range must be big enough to travel 2% easily
    "atr_pct_max": 9.0,       # ... but not so big that the stop is unaffordable
    "ret5_min": 2.0,          # 5-session momentum: the stock is already moving
    "min_score": 40,          # keep the scanner's own quality floor
    # plan
    "stop_atr_mult": 2.0,     # stop = 2 x ATR below entry ...
    "stop_pct_cap": 6.0,      # ... but never wider than 6%
    "t1_pct": 2.0,            # quick target: the 2% the user actually wants
    "t2_pct": 4.0,            # runner target
    "t1_book_frac": 0.35,     # book ~1/3 at Target 1 (measured best of 0.33/0.5/1.0)
    "hold_sessions": 5,       # hard time exit
}


def gates(stat, cfg=None):
    """Return (ok, reasons) for the fast-move gates on one scanner stat dict."""
    c = cfg or CONFIG
    fails = []
    atrp = stat.get("atr_pct") or 0.0
    r5 = stat.get("r5")
    if atrp < c["atr_pct_min"]:
        fails.append(f"ATR {atrp:.1f}% < {c['atr_pct_min']:.1f}%")
    if atrp > c["atr_pct_max"]:
        fails.append(f"ATR {atrp:.1f}% > {c['atr_pct_max']:.1f}%")
    if r5 is None or r5 < c["ret5_min"]:
        fails.append(f"5-day move {0 if r5 is None else r5:+.1f}% < +{c['ret5_min']:.1f}%")
    if (stat.get("total") or 0) < c["min_score"]:
        fails.append(f"score {stat.get('total', 0):.0f} < {c['min_score']}")
    if stat.get("red_big") or stat.get("drop_big"):
        fails.append("distribution / breakdown pattern")
    return (not fails), fails


def velocity_score(stat, cfg=None):
    """Ranking score for fast-move mode: volatility x momentum x volume."""
    c = cfg or CONFIG
    atrp = stat.get("atr_pct") or 0.0
    r5 = max(0.0, min(15.0, stat.get("r5") or 0.0))
    vr = stat.get("vol_ratio_5_20") or 1.0
    return atrp * (1 + r5 / 10.0) * (0.8 + 0.4 * min(vr, 2.0))


def fast_plan(stat, capital, risk_pct, cfg=None, size_mode="risk"):
    """Entry / stop / targets / size for a fast 2% trade.

    size_mode="risk"     : position sized so a stop-out costs `risk_pct` of capital
                           (this is the honest default, but with a 5-6% stop the
                           position ends up small).
    size_mode="notional" : position capped at 30% of capital regardless of the
                           stop, i.e. bigger size and bigger rupee risk per trade.
    """
    c = cfg or CONFIG
    entry = float(stat["close"])
    atr_val = float(stat["atr"])
    if entry <= 0 or atr_val <= 0:
        return None
    stop_pct = min(c["stop_pct_cap"], c["stop_atr_mult"] * atr_val / entry * 100.0)
    sl = entry * (1 - stop_pct / 100.0)
    risk_per_share = entry - sl
    if risk_per_share <= 0:
        return None

    risk_rs = capital * risk_pct / 100.0
    qty_risk = int(risk_rs // risk_per_share)
    qty_notional = int((capital * 0.30) // entry)
    qty = qty_notional if size_mode == "notional" else qty_risk
    if qty <= 0:
        return None
    notional = entry * qty
    if notional > capital * 0.95:
        qty = int((capital * 0.95) // entry)
        notional = entry * qty
    if qty <= 0:
        return None

    t1 = entry * (1 + c["t1_pct"] / 100.0)
    t2 = entry * (1 + c["t2_pct"] / 100.0)
    t1_qty = max(1, int(round(qty * c["t1_book_frac"])))
    t1_qty = min(t1_qty, qty)
    return {
        "entry": entry, "sl": sl, "sl_pct": stop_pct,
        "t1": t1, "t2": t2,
        "t1_pct": c["t1_pct"], "t2_pct": c["t2_pct"],
        "qty": qty, "t1_qty": t1_qty, "t2_qty": qty - t1_qty,
        "notional": notional, "notional_pct": notional / capital * 100.0,
        "risk_rs": qty * risk_per_share,
        "risk_pct_actual": qty * risk_per_share / capital * 100.0,
        "rr_t1": c["t1_pct"] / stop_pct if stop_pct else 0,
        "rr_t2": c["t2_pct"] / stop_pct if stop_pct else 0,
        "hold_sessions": c["hold_sessions"],
        "mode": "fast-move",
        "size_mode": size_mode,
    }


def describe(cfg=None):
    c = cfg or CONFIG
    return (f"ATR% >= {c['atr_pct_min']:.1f}, 5-day move >= "
            f"+{c['ret5_min']:.1f}%, stop {c['stop_atr_mult']:.1f}xATR (max "
            f"{c['stop_pct_cap']:.0f}%), T1 +{c['t1_pct']:.1f}% (book "
            f"{c['t1_book_frac']*100:.0f}%), T2 +{c['t2_pct']:.1f}%, exit by "
            f"session {c['hold_sessions']}")
