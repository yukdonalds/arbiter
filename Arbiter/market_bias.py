from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import math

import config


def _ema(values: list[float], period: int) -> list[float]:
    if period <= 1:
        return list(values)
    out: list[float] = []
    alpha = 2.0 / (period + 1.0)
    ema = None
    for v in values:
        if ema is None:
            ema = v
        else:
            ema = alpha * v + (1 - alpha) * ema
        out.append(float(ema))
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _strength_from_spread(price: float, spread: float) -> float:
    """
    Map EMA spread as % of price to 0..1. Uses tanh for smooth saturation.
    Typical regime: spread_pct ~ 0.1%..1.5% => strength ~ 0.1..0.9
    """
    if price <= 0:
        return 0.0
    spread_pct = abs(spread) / price * 100.0
    # 0.0% -> 0, 2.0% -> ~0.96
    return float(_clamp(math.tanh(spread_pct / 2.0), 0.0, 1.0))


def get_market_bias_from_closes(closes: Iterable[float]) -> dict:
    closes_list = [float(x) for x in closes if x is not None]
    if len(closes_list) < max(config.BIAS_EMA_FAST, config.BIAS_EMA_SLOW) + 3:
        return {"direction": "NEUTRAL", "strength": 0.0}

    ema_fast = _ema(closes_list, int(config.BIAS_EMA_FAST))
    ema_slow = _ema(closes_list, int(config.BIAS_EMA_SLOW))
    f = ema_fast[-1]
    s = ema_slow[-1]
    spread = f - s

    # Slope filter reduces whipsaw: require fast EMA to be moving in direction.
    lookback = int(getattr(config, "BIAS_SLOPE_LOOKBACK", 5))
    if lookback < 1 or len(ema_fast) < lookback + 1:
        slope = 0.0
    else:
        slope = ema_fast[-1] - ema_fast[-(lookback + 1)]

    if spread > 0 and slope > 0:
        direction = "LONG"
    elif spread < 0 and slope < 0:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
        # Don't block all trades when EMA ~ flat: fallback so bias "biases" not "gates"
        fallback = getattr(config, "BIAS_NEUTRAL_FALLBACK", None)
        if fallback and str(fallback).upper() in ("LONG", "SHORT"):
            direction = str(fallback).upper()
            strength = 0.5
            return {"direction": direction, "strength": strength}

    strength = _strength_from_spread(closes_list[-1], spread)
    return {"direction": direction, "strength": strength}


def get_market_bias(etf_closed_bars: list[dict] | None) -> dict:
    """
    Accepts BarBuilder closed bars for ETF: [{"close": ..., "time_et": ...}, ...]
    """
    if not etf_closed_bars:
        return {"direction": "NEUTRAL", "strength": 0.0}
    closes = [float(b.get("close") or 0.0) for b in etf_closed_bars if (b.get("close") or 0) > 0]
    return get_market_bias_from_closes(closes)

