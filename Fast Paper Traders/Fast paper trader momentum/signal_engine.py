# -----------------------------
# Fast Paper Trader – v2.6 signal engine (copy from mission)
# -----------------------------
import config

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
) -> tuple[bool, float]:
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
    liquidity_ok = avg_vol >= config.MIN_AVG_DAILY_VOLUME
    price_ok = config.PRICE_MIN <= close <= config.PRICE_MAX
    pct_change_1d = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    rel_vol = (today_vol / avg_vol) if avg_vol else 0.0
    momentum_ok = (
        pct_change_1d >= config.MIN_PCT_CHANGE_1D
        and rel_vol >= config.MIN_RELATIVE_VOLUME
        and atr_pct >= config.ATR_PCT_MIN
    )
    volatility_ok = config.ATR_PCT_MIN <= atr_pct <= config.ATR_PCT_MAX
    vwap = (high + low + close) / 3.0
    dist_vwap = (close - vwap) / vwap * 100 if vwap else 0.0
    structural_ok = close > vwap and dist_vwap <= config.MAX_DISTANCE_FROM_VWAP_PCT

    eligible = all([liquidity_ok, price_ok, momentum_ok, volatility_ok, structural_ok])
    a, b, c = config.SCORE_WEIGHTS
    score = pct_change_1d * a + (rel_vol - 1.0) * b + atr_pct * c
    return eligible, score

def rank_and_cap(signals: list[dict], max_n: int = 10) -> list[dict]:
    sorted_s = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)
    return sorted_s[:max_n]
