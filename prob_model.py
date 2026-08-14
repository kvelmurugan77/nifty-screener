#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prob_model.py — historical probability of a pick reaching Target 1.

Trained on a 2-year backtest of this exact strategy (3,081 signals, 148 NSE
stocks). The model is a small logistic regression on features the scanner
already computes. It is CALIBRATED (predicted % tracks actual hit rate), but
it is an HONEST HISTORICAL ESTIMATE — not a guarantee, not investment advice.

Typical output: ~25% to ~35%. Base rate is ~29% (i.e., historically ~1 in 3
picks reached Target 1).

Usage:
    from prob_model import success_probability
    p = success_probability(score=100, rsi=60, dist_52h=-2.2,
                            vol_ratio=2.0, atr_pct=2.5, regime_bull=False)
    -> 0.336  (33.6%)
"""

import json
import math
import os

_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prob_model.json")

_FEATURES = ["score", "rsi", "dist_52h", "vol_ratio", "atr_pct", "regime_bull"]

_MODEL = None


def _load():
    global _MODEL
    if _MODEL is None:
        with open(_MODEL_PATH) as fh:
            _MODEL = json.load(fh)
    return _MODEL


def success_probability(score, rsi, dist_52h, vol_ratio, atr_pct, regime_bull):
    """Return float 0..1 = estimated probability of reaching Target 1.

    regime_bull: True if NIFTY is above its 50-day average.
    """
    m = _load()
    vals = [score, rsi, dist_52h, vol_ratio, atr_pct, int(bool(regime_bull))]
    z = m["intercept"]
    for name, v in zip(_FEATURES, vals):
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            v = m["means"][name]
        z += m["coefs"][name] * (v - m["means"][name]) / m["stds"][name]
    p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    return p


def format_probability(score, rsi, dist_52h, vol_ratio, atr_pct, regime_bull):
    """Return a short, honest human string like '~34% (1 in 3)'."""
    p = success_probability(score, rsi, dist_52h, vol_ratio, atr_pct, regime_bull)
    pct = int(round(p * 100))
    if pct >= 33:
        note = "roughly 1 in 3"
    elif pct >= 28:
        note = "slightly above 1 in 4"
    else:
        note = "about 1 in 4"
    return f"~{pct}% ({note}) based on a 2-year backtest of similar setups"
