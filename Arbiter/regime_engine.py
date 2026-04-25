# -----------------------------
# Minimal SPY regime state (sizing multiplier only — no signal logic)
# -----------------------------
"""
Uses only ETF OHLC aggregate bars already in BarBuilder:

1) Trend magnitude — abs(N-bar return), direction-agnostic persistence
2) Volatility — ATR vs rolling median baseline (compression / grind / expansion)
3) Efficiency ratio — net move / sum |Δclose| (path efficiency; low = chop)

Combined into regime_score ∈ [0, 1], mapped to size_multiplier bands per config.
"""
from __future__ import annotations

import math
from typing import Any

import config


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _true_range(h: float, l: float, prev_close: float) -> float:
    if h <= 0 or l <= 0:
        return 0.0
    if prev_close > 0:
        return max(h - l, abs(h - prev_close), abs(l - prev_close))
    return max(h - l, 0.0)


def _atr_wilder_series(bars: list[dict], period: int) -> list[float]:
    """Wilder ATR per bar index; NaN until first seeded value at index `period`."""
    L = len(bars)
    atr = [math.nan] * L
    if period < 1 or L < period + 1:
        return atr
    tr = [0.0] * L
    tr[0] = max(
        float(bars[0].get("high") or 0.0) - float(bars[0].get("low") or 0.0),
        0.0,
    )
    for i in range(1, L):
        h = float(bars[i].get("high") or 0.0)
        l = float(bars[i].get("low") or 0.0)
        pc = float(bars[i - 1].get("close") or 0.0)
        tr[i] = _true_range(h, l, pc)
    # First ATR = average of TR[1]..TR[period]
    atr[period] = sum(tr[1 : period + 1]) / float(period)
    alpha = 1.0 / float(period)
    for i in range(period + 1, L):
        atr[i] = atr[i - 1] + alpha * (tr[i] - atr[i - 1])
    return atr


def _efficiency_ratio(closes: list[float], n: int) -> float:
    if n < 2 or len(closes) < n + 1:
        return 0.5
    num = abs(closes[-1] - closes[-1 - n])
    den = sum(abs(closes[-1 - i] - closes[-2 - i]) for i in range(n))
    if den <= 1e-12:
        return 0.5
    return float(_clamp(num / den, 0.0, 1.0))


def _trend_strength_score(closes: list[float], n: int, scale_pct: float) -> float:
    """Map abs(n-bar return %) to [0, 1]."""
    if len(closes) < n + 1 or scale_pct <= 0:
        return 0.5
    pc = float(closes[-1 - n])
    if pc <= 0:
        return 0.5
    ret_pct = abs(closes[-1] / pc - 1.0) * 100.0
    return float(_clamp(ret_pct / scale_pct, 0.0, 1.0))


def _volatility_score(atr_series: list[float], baseline_len: int) -> float:
    """Latest ATR vs median ATR over baseline window → [0, 1], ~0.5 when ratio ≈ 1."""
    bl = max(2, int(baseline_len))
    valid_ns: list[float] = [x for x in atr_series if x == x and x > 0.0]
    if len(valid_ns) < bl:
        return 0.5
    tail = valid_ns[-bl:]
    cur = tail[-1]
    hist = tail[:-1]
    if not hist:
        return 0.5
    base = _median(hist)
    if base <= 0:
        return 0.5
    ratio = cur / base
    return float(_clamp(0.5 + 0.5 * math.tanh(math.log(max(ratio, 0.05))), 0.0, 1.0))


def size_multiplier_from_regime_score(score: float) -> float:
    """Piecewise linear bands (chop → grind → trend → expansion)."""
    s = _clamp(float(score), 0.0, 1.0)
    if s < 0.35:
        t = s / 0.35
        return 0.3 + t * 0.3
    if s < 0.65:
        t = (s - 0.35) / 0.30
        return 0.7 + t * 0.30
    if s < 0.85:
        t = (s - 0.65) / 0.20
        return 1.1 + t * 0.30
    t = _clamp((s - 0.85) / 0.15, 0.0, 1.0)
    return 1.4 + t * 0.10


def _label_from_score(score: float) -> str:
    if score < 0.35:
        return "CHOP"
    if score < 0.65:
        return "GRIND"
    if score < 0.85:
        return "TREND"
    return "EXPANSION"


def bars_from_builder(bar_builder: Any, symbol: str) -> list[dict]:
    """Closed bars + forming bar (same convention as market bias)."""
    closed = bar_builder.get_all_closed(symbol)
    bars = [dict(b) for b in closed]
    cur = bar_builder.get_current_bar(symbol)
    if cur and (cur.get("close") or 0) > 0:
        bars.append(dict(cur))
    return bars


def compute_regime_from_barbuilder(bar_builder: Any, symbol: str | None = None) -> dict[str, Any]:
    """
    Return regime_score, label, components, and size_multiplier for ETF symbol.
    On insufficient data: neutral score 0.5, multiplier ~0.85 (grind mid).
    """
    sym = (symbol or getattr(config, "ETF_SYMBOL", "SPY")).upper()
    bars = bars_from_builder(bar_builder, sym)
    wt = float(getattr(config, "REGIME_WEIGHT_TREND", 1.0))
    wv = float(getattr(config, "REGIME_WEIGHT_VOL", 1.0))
    we = float(getattr(config, "REGIME_WEIGHT_ER", 1.0))
    wsum = wt + wv + we
    if wsum <= 0:
        wsum = 1.0

    trend_n = int(getattr(config, "REGIME_TREND_LOOKBACK", 40))
    er_n = int(getattr(config, "REGIME_ER_LOOKBACK", 40))
    atr_p = int(getattr(config, "REGIME_ATR_PERIOD", 14))
    base_n = int(getattr(config, "REGIME_VOL_BASELINE_BARS", 64))
    trend_scale = float(getattr(config, "REGIME_TREND_SCALE_PCT", 1.25))

    min_len = max(trend_n + 1, er_n + 1, atr_p + base_n + 1)
    if len(bars) < min_len:
        sm = size_multiplier_from_regime_score(0.5)
        return {
            "symbol": sym,
            "regime_score": 0.5,
            "label": "GRIND",
            "size_multiplier": sm,
            "trend_score": 0.5,
            "vol_score": 0.5,
            "efficiency_score": 0.5,
            "insufficient_bars": True,
        }

    closes = []
    for b in bars:
        c = float(b.get("close") or 0.0)
        if c <= 0:
            sm = size_multiplier_from_regime_score(0.5)
            return {
                "symbol": sym,
                "regime_score": 0.5,
                "label": "GRIND",
                "size_multiplier": sm,
                "trend_score": 0.5,
                "vol_score": 0.5,
                "efficiency_score": 0.5,
                "insufficient_bars": True,
            }
        closes.append(c)
    if len(closes) < min_len:
        sm = size_multiplier_from_regime_score(0.5)
        return {
            "symbol": sym,
            "regime_score": 0.5,
            "label": "GRIND",
            "size_multiplier": sm,
            "trend_score": 0.5,
            "vol_score": 0.5,
            "efficiency_score": 0.5,
            "insufficient_bars": True,
        }

    # Align TR/ATR with bar list (use same subset as closes — bars must match closes indexing)
    atr_series = _atr_wilder_series(bars, atr_p)
    ts = _trend_strength_score(closes, trend_n, trend_scale)
    vs = _volatility_score(atr_series, base_n)
    er = _efficiency_ratio(closes, er_n)

    regime_score = (wt * ts + wv * vs + we * er) / wsum
    regime_score = float(_clamp(regime_score, 0.0, 1.0))

    sm = size_multiplier_from_regime_score(regime_score)
    return {
        "symbol": sym,
        "regime_score": regime_score,
        "label": _label_from_score(regime_score),
        "size_multiplier": sm,
        "trend_score": ts,
        "vol_score": vs,
        "efficiency_score": er,
        "insufficient_bars": False,
    }
