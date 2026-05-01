"""
Fast Paper Long/Short – v2.6 signal engine

Long signal is identical to your v2.6 long-only version.
Short signal mirrors the same constraints:
- momentum uses negative pct_change_1d
- structural uses close < VWAP
- VWAP distance uses absolute bound
"""

import math

import config


def _safe_float(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)


def compute_trade_score(
    rel_vol: float,
    atr_dec: float,
    breakout_dec: float,
    momentum_ok: bool,
    structure_ok: bool,
) -> float:
    """
    Score in [0, 100] from normalized inputs.
    - rel_vol is ratio (e.g. 1.4x)
    - atr_dec is decimal (e.g. 1.2% -> 0.012)
    - breakout_dec is decimal (e.g. 0.5% -> 0.005)
    """
    score = 0.0
    score += min(max(float(rel_vol or 0.0), 0.0) * 20.0, 25.0)     # Volume (0-25)
    score += min(max(float(atr_dec or 0.0), 0.0) * 1500.0, 25.0)    # Volatility (0-25)
    score += min(max(float(breakout_dec or 0.0), 0.0) * 2000.0, 20.0)  # Breakout (0-20)
    if momentum_ok:
        score += 15.0
    if structure_ok:
        score += 15.0
    return float(score)


def expected_move_ok(expected_move_pct: float) -> bool:
    min_expected = float(getattr(config, "MIN_EXPECTED_MOVE_PCT", 0.005) or 0.005)
    return float(expected_move_pct or 0.0) >= min_expected


def atr_ok(atr_pct: float) -> bool:
    # atr_pct in pipeline is percent-units (e.g. 1.2 means 1.2%).
    min_atr_decimal = float(getattr(config, "MIN_ATR_PCT", 0.008) or 0.008)
    min_atr_percent_units = min_atr_decimal * 100.0
    return float(atr_pct or 0.0) >= min_atr_percent_units


def _time_adjusted_rel_vol(today_vol: float, avg_vol: float, minutes_since_open: float) -> float:
    minutes_since_open = max(1.0, float(minutes_since_open or 1.0))
    expected_volume = float(avg_vol) * (minutes_since_open / 390.0)
    return float(today_vol) / max(expected_volume, 1.0)


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
    thresholds: dict,
) -> None:
    if not getattr(config, "DEBUG_SIGNAL_CONDITIONS", False):
        return
    print(
        f"{ticker} {side} | "
        f"L:{liquidity_ok} P:{price_ok} M:{momentum_ok} V:{volatility_ok} VW:{structural_ok} | "
        f"avg_vol_20={avg_vol:.0f} today_vol={today_vol:.0f} rel_vol={rel_vol:.2f} "
        f"pct_1d={pct_change_1d:.2f} atr%={atr_pct:.2f} dist_vwap%={dist_vwap:.2f} | "
        f"thr(avg={thresholds['MIN_AVG_DAILY_VOLUME']}, spike={thresholds['LIQUIDITY_VOLUME_SPIKE_MIN']}, "
        f"pct={thresholds['MIN_PCT_CHANGE_1D']}, rel={thresholds['MIN_RELATIVE_VOLUME']}, "
        f"atr_min={thresholds['ATR_PCT_MIN']})"
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
    close = _safe_float(bar.get("close"), 0.0)
    high = bar.get("high") or close
    low = bar.get("low") or close
    vol = bar.get("volume") or 0.0
    avg_vol = daily_metrics.get("avg_vol_20") or 0.0
    prev_close = daily_metrics.get("prev_close") or close
    atr_pct = _safe_float(daily_metrics.get("atr_pct"), 0.0)
    today_vol = daily_metrics.get("today_volume_so_far", 0.0) + vol

    if close <= 0:
        return False, 0.0
    thr = _debug_thresholds()
    # Tuned confirmation thresholds (do not rely on config.MIN_* values here).
    thr["MIN_PCT_CHANGE_1D"] = 0.3
    thr["MIN_RELATIVE_VOLUME"] = float(getattr(config, "MIN_VOLUME_MULTIPLIER", 1.0) or 1.0)
    # Liquidity: ticker has minimum average volume AND today has a small volume spike (config.LIQUIDITY_VOLUME_SPIKE_MIN, e.g. 1.2)
    liquidity_ok = (
        avg_vol >= thr["MIN_AVG_DAILY_VOLUME"]
        and (today_vol >= thr["LIQUIDITY_VOLUME_SPIKE_MIN"] * avg_vol if avg_vol else False)
    )
    price_ok = config.PRICE_MIN <= close <= config.PRICE_MAX
    pct_change_1d = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    minutes_since_open = _safe_float(daily_metrics.get("minutes_since_market_open", 390.0), 390.0)
    rel_vol = _time_adjusted_rel_vol(today_vol, avg_vol, minutes_since_open) if avg_vol else 0.0
    # Tuned momentum:
    # - Base: require pct_change_1d >= 0.3% and rel_vol >= MIN_VOLUME_MULTIPLIER
    # - Relax pct_change_1d to 0.2% ONLY when rel_vol > 1.5x OR ADX > 30
    adx = _safe_float(bar.get("adx"), 0.0)
    strong_relax = (rel_vol > 1.5) or (adx > 30.0)
    pct_thr = 0.2 if strong_relax else float(thr["MIN_PCT_CHANGE_1D"] or 0.3)
    rv_thr = float(thr["MIN_RELATIVE_VOLUME"] or 1.0)

    strong_momentum = (pct_change_1d >= pct_thr) and (rel_vol >= rv_thr)
    momentum_ok = strong_momentum
    volatility_ok = thr["ATR_PCT_MIN"] <= atr_pct <= thr["ATR_PCT_MAX"]
    vwap = (high + low + close) / 3.0
    dist_vwap = (close - vwap) / vwap * 100 if vwap else 0.0
    structural_ok = adx > 25.0

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

    rel_vol = max(0.0, _safe_float(rel_vol, 0.0))
    atr_dec = max(0.0, _safe_float(atr_pct, 0.0)) / 100.0
    dist_vwap = _safe_float(dist_vwap, 0.0)
    breakout_dec = abs(dist_vwap) / 100.0
    pct_change_1d = _safe_float(pct_change_1d, 0.0)
    adx = _safe_float(adx, 0.0)
    structure_ok = adx > 25.0
    score = compute_trade_score(rel_vol, atr_dec, breakout_dec, momentum_ok, structure_ok)
    eligible = (
        liquidity_ok
        and price_ok
        and volatility_ok
        and expected_move_ok(max(abs(float(pct_change_1d or 0.0)) / 100.0, atr_dec))
        and atr_ok(atr_pct)
    )
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

    close = _safe_float(bar.get("close"), 0.0)
    high = bar.get("high") or close
    low = bar.get("low") or close
    vol = bar.get("volume") or 0.0
    avg_vol = daily_metrics.get("avg_vol_20") or 0.0
    prev_close = daily_metrics.get("prev_close") or close
    atr_pct = _safe_float(daily_metrics.get("atr_pct"), 0.0)
    today_vol = daily_metrics.get("today_volume_so_far", 0.0) + vol

    if close <= 0:
        return False, 0.0
    thr = _debug_thresholds()
    # Tuned confirmation thresholds (do not rely on config.MIN_* values here).
    thr["MIN_PCT_CHANGE_1D"] = 0.3
    thr["MIN_RELATIVE_VOLUME"] = float(getattr(config, "MIN_VOLUME_MULTIPLIER", 1.0) or 1.0)
    # Liquidity: ticker has minimum average volume AND today has a small volume spike (config.LIQUIDITY_VOLUME_SPIKE_MIN, e.g. 1.2)
    liquidity_ok = (
        avg_vol >= thr["MIN_AVG_DAILY_VOLUME"]
        and (today_vol >= thr["LIQUIDITY_VOLUME_SPIKE_MIN"] * avg_vol if avg_vol else False)
    )
    price_ok = config.PRICE_MIN <= close <= config.PRICE_MAX
    pct_change_1d = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    minutes_since_open = _safe_float(daily_metrics.get("minutes_since_market_open", 390.0), 390.0)
    rel_vol = _time_adjusted_rel_vol(today_vol, avg_vol, minutes_since_open) if avg_vol else 0.0

    # Tiered momentum (shorts): negative move with relative volume.
    adx = _safe_float(bar.get("adx"), 0.0)
    strong_relax = (rel_vol > 1.5) or (adx > 30.0)
    pct_thr = 0.2 if strong_relax else float(thr["MIN_PCT_CHANGE_1D"] or 0.3)
    rv_thr = float(thr["MIN_RELATIVE_VOLUME"] or 1.0)

    strong_momentum = (pct_change_1d <= -pct_thr) and (rel_vol >= rv_thr)
    momentum_ok = strong_momentum
    volatility_ok = thr["ATR_PCT_MIN"] <= atr_pct <= thr["ATR_PCT_MAX"]
    vwap = (high + low + close) / 3.0
    dist_vwap = (close - vwap) / vwap * 100 if vwap else 0.0
    structural_ok = adx > 25.0

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

    rel_vol = max(0.0, _safe_float(rel_vol, 0.0))
    atr_dec = max(0.0, _safe_float(atr_pct, 0.0)) / 100.0
    dist_vwap = _safe_float(dist_vwap, 0.0)
    breakout_dec = abs(dist_vwap) / 100.0
    pct_change_1d = _safe_float(pct_change_1d, 0.0)
    adx = _safe_float(adx, 0.0)
    structure_ok = adx > 25.0
    score = compute_trade_score(rel_vol, atr_dec, breakout_dec, momentum_ok, structure_ok)
    eligible = (
        liquidity_ok
        and price_ok
        and volatility_ok
        and expected_move_ok(max(abs(float(pct_change_1d or 0.0)) / 100.0, atr_dec))
        and atr_ok(atr_pct)
    )
    return eligible, score


def rank_and_cap(signals: list[dict], max_n: int = 10) -> list[dict]:
    """
    Prioritize by score and keep only top configured candidates.
    """
    sorted_s = sorted(signals, key=lambda x: float(x.get("score", 0.0) or 0.0), reverse=True)
    top_take = int(getattr(config, "TOP_TRADES_TO_TAKE", 3) or 3)
    effective_cap = min(max_n, max(1, top_take))
    return sorted_s[:effective_cap]

