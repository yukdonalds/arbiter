"""
Fast Paper Long/Short – v2.6 signal engine

Long signal is identical to your v2.6 long-only version.
Short signal mirrors the same constraints:
- momentum uses negative pct_change_1d
- structural uses close < VWAP
- VWAP distance uses absolute bound
"""

import config


def _debug_thresholds():
    """
    Optional one-session relax to confirm signals can fire.
    Returns a dict of effective thresholds.
    """
    if getattr(config, "DEBUG_RELAX_FILTERS", False):
        return {
            "MIN_AVG_DAILY_VOLUME": 200_000,
            "LIQUIDITY_VOLUME_SPIKE_MIN": 1.0,
            "MIN_PCT_CHANGE_1D": 0.3,
            "MIN_RELATIVE_VOLUME": 1.0,
            "ATR_PCT_MIN": 0.5,
            "ATR_PCT_MAX": getattr(config, "ATR_PCT_MAX", 10.0),
            "MAX_DISTANCE_FROM_VWAP_PCT": 999.0,  # effectively disables VWAP distance cap
        }
    return {
        "MIN_AVG_DAILY_VOLUME": config.MIN_AVG_DAILY_VOLUME,
        "LIQUIDITY_VOLUME_SPIKE_MIN": config.LIQUIDITY_VOLUME_SPIKE_MIN,
        "MIN_PCT_CHANGE_1D": config.MIN_PCT_CHANGE_1D,
        "MIN_RELATIVE_VOLUME": config.MIN_RELATIVE_VOLUME,
        "ATR_PCT_MIN": config.ATR_PCT_MIN,
        "ATR_PCT_MAX": config.ATR_PCT_MAX,
        "MAX_DISTANCE_FROM_VWAP_PCT": config.MAX_DISTANCE_FROM_VWAP_PCT,
    }


def _safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b


def _trend_score(pct_change_1d: float, rel_vol: float, pct_thr: float, rv_thr: float) -> float:
    """
    0..1 trend quality proxy:
    - directional move component vs configured move threshold
    - participation component vs configured relative-volume threshold
    """
    move_component = max(0.0, min(1.0, _safe_div(abs(pct_change_1d), max(pct_thr, 0.01))))
    vol_component = max(0.0, min(1.0, _safe_div(rel_vol, max(rv_thr, 0.01))))
    return 0.5 * move_component + 0.5 * vol_component


def _maybe_print_debug(
    ticker: str,
    side: str,
    liquidity_ok: bool,
    price_ok: bool,
    momentum_ok: bool,
    volatility_ok: bool,
    structural_ok: bool,
    avg_vol: float,
    today_vol: float,
    pct_change_1d: float,
    rel_vol: float,
    atr_pct: float,
    dist_vwap: float,
    move_pct: float,
    trend_score: float,
    expected_range_pct: float,
    confirmation_ok: bool,
    thresholds: dict,
) -> None:
    if not getattr(config, "DEBUG_SIGNAL_CONDITIONS", False):
        return
    print(
        f"{ticker} {side} | "
        f"L:{liquidity_ok} P:{price_ok} M:{momentum_ok} V:{volatility_ok} VW:{structural_ok} | "
        f"avg_vol_20={avg_vol:.0f} today_vol={today_vol:.0f} rel_vol={rel_vol:.2f} "
        f"pct_1d={pct_change_1d:.2f} atr%={atr_pct:.2f} dist_vwap%={dist_vwap:.2f} "
        f"move%={move_pct:.2f} trend={trend_score:.2f} range%={expected_range_pct:.2f} confirm={confirmation_ok} | "
        f"thr(avg={thresholds['MIN_AVG_DAILY_VOLUME']}, spike={thresholds['LIQUIDITY_VOLUME_SPIKE_MIN']}, "
        f"pct={thresholds['MIN_PCT_CHANGE_1D']}, rel={thresholds['MIN_RELATIVE_VOLUME']}, "
        f"atr_min={thresholds['ATR_PCT_MIN']}, move={getattr(config, 'MIN_MOVE_PCT', 0.0)}, "
        f"vol_mult={getattr(config, 'MIN_VOLUME_MULTIPLIER', 0.0)}, trend={getattr(config, 'MIN_TREND_SCORE', 0.0)}, "
        f"range={getattr(config, 'MIN_EXPECTED_RANGE', 0.0)})"
    )


def atr_pct_from_bars(bars_high_low_close, period=14):
    if len(bars_high_low_close) < period + 1:
        return 0.0
    tr_list = []
    for j in range(1, period + 1):
        i = -j
        h, l_, c = bars_high_low_close[i]
        prev_c = bars_high_low_close[i - 1][2]
        tr = max(h - l_, abs(h - prev_c), abs(l_ - prev_c))
        tr_list.append(tr)
    atr = sum(tr_list) / len(tr_list)
    close = bars_high_low_close[-1][2] or 1.0
    return (atr / close * 100) if close else 0.0


def check_v26_bar(
    ticker: str,
    bar: dict,
    daily_metrics: dict,
    condition_counts: dict | None = None,
) -> tuple[bool, float]:
    """LONG-only v2.6 check. If condition_counts dict provided, increment per-condition hits."""
    close = bar.get("close") or 0.0
    high = bar.get("high") or close
    low = bar.get("low") or close
    vol = bar.get("volume") or 0.0
    avg_vol = daily_metrics.get("avg_vol_20") or 0.0
    prev_close = daily_metrics.get("prev_close") or close
    atr_pct = daily_metrics.get("atr_pct") or 0.0
    today_vol = daily_metrics.get("today_volume_so_far", 0.0) + vol

    if close <= 0:
        return False, 0.0
    thr = _debug_thresholds()
    # Liquidity: ticker has minimum average volume AND today has a small volume spike (config.LIQUIDITY_VOLUME_SPIKE_MIN, e.g. 1.2)
    liquidity_ok = (
        avg_vol >= thr["MIN_AVG_DAILY_VOLUME"]
        and (today_vol >= thr["LIQUIDITY_VOLUME_SPIKE_MIN"] * avg_vol if avg_vol else False)
    )
    price_ok = config.PRICE_MIN <= close <= config.PRICE_MAX
    pct_change_1d = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    rel_vol = (today_vol / avg_vol) if avg_vol else 0.0
    # Tiered momentum:
    # - Strong: meets the configured thresholds.
    # - Moderate: ~50% of pct threshold and ~70% of rel-vol threshold (still counts toward eligibility).
    # Momentum is not intended to be a hard gate; other conditions can carry eligibility.
    pct_thr = float(thr["MIN_PCT_CHANGE_1D"] or 0.0)
    rv_thr = float(thr["MIN_RELATIVE_VOLUME"] or 0.0)
    strong_momentum = (pct_change_1d >= pct_thr) and (rel_vol >= rv_thr)
    moderate_momentum = (pct_change_1d >= 0.5 * pct_thr) and (rel_vol >= 0.7 * rv_thr)
    momentum_ok = strong_momentum or moderate_momentum
    volatility_ok = thr["ATR_PCT_MIN"] <= atr_pct <= thr["ATR_PCT_MAX"]
    vwap = (high + low + close) / 3.0
    dist_vwap = (close - vwap) / vwap * 100 if vwap else 0.0
    structural_ok = close > vwap and dist_vwap <= thr["MAX_DISTANCE_FROM_VWAP_PCT"]
    # Strong-entry filters (confirmation / trend quality / range).
    open_price = float(bar.get("open") or close)
    range_pct = ((high - low) / close * 100.0) if close else 0.0
    move_pct = ((close - open_price) / open_price * 100.0) if open_price else 0.0
    trend_score = _trend_score(pct_change_1d, rel_vol, pct_thr, rv_thr)
    confirmation_ok = close >= (high - 0.2 * (high - low)) and close >= open_price and close >= prev_close
    move_ok = move_pct >= float(getattr(config, "MIN_MOVE_PCT", 0.0))
    volume_ok = rel_vol >= float(getattr(config, "MIN_VOLUME_MULTIPLIER", 0.0))
    trend_ok = trend_score >= float(getattr(config, "MIN_TREND_SCORE", 0.0))
    range_ok = range_pct >= float(getattr(config, "MIN_EXPECTED_RANGE", 0.0))
    quality_ok = move_ok and volume_ok and trend_ok and range_ok and confirmation_ok

    _maybe_print_debug(
        ticker,
        "LONG",
        liquidity_ok,
        price_ok,
        momentum_ok,
        volatility_ok,
        structural_ok,
        float(avg_vol or 0.0),
        float(today_vol or 0.0),
        float(pct_change_1d or 0.0),
        float(rel_vol or 0.0),
        float(atr_pct or 0.0),
        float(dist_vwap or 0.0),
        float(move_pct or 0.0),
        float(trend_score or 0.0),
        float(range_pct or 0.0),
        bool(confirmation_ok),
        thr,
    )

    if condition_counts is not None:
        if liquidity_ok:
            condition_counts["liquidity"] = condition_counts.get("liquidity", 0) + 1
        if price_ok:
            condition_counts["price"] = condition_counts.get("price", 0) + 1
        if momentum_ok:
            condition_counts["momentum"] = condition_counts.get("momentum", 0) + 1
        if volatility_ok:
            condition_counts["volatility"] = condition_counts.get("volatility", 0) + 1
        if structural_ok:
            condition_counts["structural"] = condition_counts.get("structural", 0) + 1

    # Scoring: signal when at least 3 of 5 conditions hold (don't require all)
    condition_score = sum([liquidity_ok, price_ok, momentum_ok, volatility_ok, structural_ok])
    eligible = (condition_score >= 3) and quality_ok
    a, b, c = config.SCORE_WEIGHTS
    # Reward strong momentum more than moderate.
    momentum_bonus = 0.0
    if strong_momentum:
        momentum_bonus = 1.0
    elif moderate_momentum:
        momentum_bonus = 0.3
    score = pct_change_1d * a + (rel_vol - 1.0) * b + atr_pct * c + momentum_bonus
    return eligible, score


def check_v26_bar_side(
    ticker: str,
    bar: dict,
    daily_metrics: dict,
    side: str,
    condition_counts: dict | None = None,
) -> tuple[bool, float]:
    """
    side: "LONG" or "SHORT"
    Returns (eligible, score).
    If condition_counts provided, increment per-condition hits (diagnostic).
    """
    side = (side or "").upper()
    if side == "LONG":
        return check_v26_bar(ticker, bar, daily_metrics, condition_counts)
    if side != "SHORT":
        return False, 0.0

    close = bar.get("close") or 0.0
    high = bar.get("high") or close
    low = bar.get("low") or close
    vol = bar.get("volume") or 0.0
    avg_vol = daily_metrics.get("avg_vol_20") or 0.0
    prev_close = daily_metrics.get("prev_close") or close
    atr_pct = daily_metrics.get("atr_pct") or 0.0
    today_vol = daily_metrics.get("today_volume_so_far", 0.0) + vol

    if close <= 0:
        return False, 0.0
    thr = _debug_thresholds()
    # Liquidity: ticker has minimum average volume AND today has a small volume spike (config.LIQUIDITY_VOLUME_SPIKE_MIN, e.g. 1.2)
    liquidity_ok = (
        avg_vol >= thr["MIN_AVG_DAILY_VOLUME"]
        and (today_vol >= thr["LIQUIDITY_VOLUME_SPIKE_MIN"] * avg_vol if avg_vol else False)
    )
    price_ok = config.PRICE_MIN <= close <= config.PRICE_MAX
    pct_change_1d = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    rel_vol = (today_vol / avg_vol) if avg_vol else 0.0

    # Tiered momentum (shorts): negative move with relative volume.
    pct_thr = float(thr["MIN_PCT_CHANGE_1D"] or 0.0)
    rv_thr = float(thr["MIN_RELATIVE_VOLUME"] or 0.0)
    strong_momentum = (pct_change_1d <= -pct_thr) and (rel_vol >= rv_thr)
    moderate_momentum = (pct_change_1d <= -(0.5 * pct_thr)) and (rel_vol >= 0.7 * rv_thr)
    momentum_ok = strong_momentum or moderate_momentum
    volatility_ok = thr["ATR_PCT_MIN"] <= atr_pct <= thr["ATR_PCT_MAX"]
    vwap = (high + low + close) / 3.0
    dist_vwap = (close - vwap) / vwap * 100 if vwap else 0.0
    structural_ok = close < vwap and abs(dist_vwap) <= thr["MAX_DISTANCE_FROM_VWAP_PCT"]
    # Strong-entry filters (confirmation / trend quality / range) for shorts.
    open_price = float(bar.get("open") or close)
    range_pct = ((high - low) / close * 100.0) if close else 0.0
    move_pct = ((open_price - close) / open_price * 100.0) if open_price else 0.0
    trend_score = _trend_score(pct_change_1d, rel_vol, pct_thr, rv_thr)
    confirmation_ok = close <= (low + 0.2 * (high - low)) and close <= open_price and close <= prev_close
    move_ok = move_pct >= float(getattr(config, "MIN_MOVE_PCT", 0.0))
    volume_ok = rel_vol >= float(getattr(config, "MIN_VOLUME_MULTIPLIER", 0.0))
    trend_ok = trend_score >= float(getattr(config, "MIN_TREND_SCORE", 0.0))
    range_ok = range_pct >= float(getattr(config, "MIN_EXPECTED_RANGE", 0.0))
    quality_ok = move_ok and volume_ok and trend_ok and range_ok and confirmation_ok

    _maybe_print_debug(
        ticker,
        "SHORT",
        liquidity_ok,
        price_ok,
        momentum_ok,
        volatility_ok,
        structural_ok,
        float(avg_vol or 0.0),
        float(today_vol or 0.0),
        float(pct_change_1d or 0.0),
        float(rel_vol or 0.0),
        float(atr_pct or 0.0),
        float(dist_vwap or 0.0),
        float(move_pct or 0.0),
        float(trend_score or 0.0),
        float(range_pct or 0.0),
        bool(confirmation_ok),
        thr,
    )

    if condition_counts is not None:
        if liquidity_ok:
            condition_counts["liquidity"] = condition_counts.get("liquidity", 0) + 1
        if price_ok:
            condition_counts["price"] = condition_counts.get("price", 0) + 1
        if momentum_ok:
            condition_counts["momentum"] = condition_counts.get("momentum", 0) + 1
        if volatility_ok:
            condition_counts["volatility"] = condition_counts.get("volatility", 0) + 1
        if structural_ok:
            condition_counts["structural"] = condition_counts.get("structural", 0) + 1

    # Scoring: signal when at least 3 of 5 conditions hold (don't require all)
    condition_score = sum([liquidity_ok, price_ok, momentum_ok, volatility_ok, structural_ok])
    eligible = (condition_score >= 3) and quality_ok
    a, b, c = config.SCORE_WEIGHTS
    # For shorts: more negative pct_change_1d is better => invert sign so "higher score is better".
    momentum_bonus = 0.0
    if strong_momentum:
        momentum_bonus = 1.0
    elif moderate_momentum:
        momentum_bonus = 0.3
    score = (-pct_change_1d) * a + (rel_vol - 1.0) * b + atr_pct * c + momentum_bonus
    return eligible, score


def rank_and_cap(signals: list[dict], max_n: int = 10) -> list[dict]:
    sorted_s = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)
    return sorted_s[:max_n]

