# -----------------------------
# Fast Paper Trader – Parallel historical data fetch (async, semaphore-limited)
# Replaces ~1hr sequential scan with a few minutes by requesting many tickers at once.
# -----------------------------
import asyncio
from datetime import datetime, timedelta
import pytz
from ib_insync import util

import config
from ib_connection import make_stock

EASTERN = pytz.timezone("America/New_York")


def _get_last_trading_day() -> datetime:
    now = datetime.now(EASTERN)
    last_day = now - timedelta(days=1)
    while last_day.weekday() >= 5:
        last_day = last_day - timedelta(days=1)
    return last_day.replace(hour=16, minute=0, second=0, microsecond=0)


def _compute_metrics_from_bars(bars) -> dict | None:
    """From daily bars, compute avg_vol_20, atr_pct, prev_close. Returns None if insufficient data."""
    if not bars or len(bars) < config.ATR_PERIOD + 1:
        return None
    df = util.df(bars)
    if len(df) < 2:
        return None
    vol = df["volume"].astype(float)
    avg_vol = vol.rolling(config.VOLUME_LOOKBACK, min_periods=1).mean().iloc[-1]
    yesterday_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2]) if len(df) >= 2 else yesterday_close
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    tr_list = []
    for j in range(1, min(config.ATR_PERIOD + 1, len(closes))):
        h, l_, c_ = highs[-j], lows[-j], closes[-j]
        prev_c = closes[-j - 1] if len(closes) > j else c_
        tr = max(h - l_, abs(h - prev_c), abs(l_ - prev_c))
        tr_list.append(tr)
    atr = sum(tr_list) / len(tr_list) if tr_list else 0.0
    atr_pct = (atr / closes[-1] * 100) if closes[-1] else 0.0
    return {
        "avg_vol_20": avg_vol,
        "atr_pct": atr_pct,
        "prev_close": prev_close,
        "yesterday_close": yesterday_close,
        "today_volume_so_far": 0.0,
    }


def _candidate_score(metrics: dict) -> float:
    """v26-style score for ranking (pct_change_1d, rel_vol proxy, atr_pct)."""
    prev = metrics.get("prev_close") or 0
    yesterday = metrics.get("yesterday_close") or prev
    if prev <= 0:
        return 0.0
    pct_1d = (yesterday - prev) / prev * 100
    rel_vol = 1.5
    a, b, c = config.SCORE_WEIGHTS
    return pct_1d * a + (rel_vol - 1.0) * b + (metrics.get("atr_pct") or 0) * c


async def _fetch_daily_bars_one(ib, sem, sym: str, end_dt_str: str) -> tuple[str, dict | None]:
    """Fetch 20 D daily bars for one symbol; return (symbol, metrics_dict or None)."""
    async with sem:
        try:
            contract = make_stock(sym)
            bars = await ib.reqHistoricalDataAsync(
                contract, end_dt_str, "20 D", "1 day", "TRADES",
                useRTH=True, timeout=12
            )
            return sym, _compute_metrics_from_bars(bars)
        except Exception:
            return sym, None


def fetch_daily_metrics_parallel(ib, tickers: list[str], max_tickers: int | None = None) -> dict:
    """
    Fetch daily metrics for many tickers in parallel (semaphore-limited).
    Returns dict: ticker -> { avg_vol_20, atr_pct, prev_close, yesterday_close, today_volume_so_far }.
    """
    last_day = _get_last_trading_day()
    end_dt_str = last_day.strftime("%Y%m%d %H:%M:%S US/Eastern")
    to_fetch = tickers[:(max_tickers or len(tickers))]
    n = len(to_fetch)
    concurrency = getattr(config, "PARALLEL_HISTORICAL_CONCURRENCY", 12)
    sem = asyncio.Semaphore(concurrency)

    async def run_all():
        tasks = [_fetch_daily_bars_one(ib, sem, sym, end_dt_str) for sym in to_fetch]
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = ib.run(run_all())
    metrics = {}
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            continue
        sym, m = r
        if m is None:
            continue
        # Basic filters so we only keep tradeable names
        if not (config.MIN_AVG_DAILY_VOLUME <= m["avg_vol_20"]):
            continue
        if not (config.PRICE_MIN <= m["yesterday_close"] <= config.PRICE_MAX):
            continue
        if not (config.ATR_PCT_MIN <= m["atr_pct"] <= config.ATR_PCT_MAX):
            continue
        metrics[sym] = m
    return metrics


def build_watchlist_parallel(ib, tickers: list[str], top_n: int = 100) -> tuple[list[str], dict]:
    """
    Fetch daily data in parallel for up to FAST_SCAN_MAX_TICKERS, rank by score, return
    (watchlist_tickers, daily_metrics_dict).
    """
    max_scan = getattr(config, "FAST_SCAN_MAX_TICKERS", 120)
    to_scan = tickers[:max_scan]
    print(f"Fast scan: fetching daily data for {len(to_scan)} tickers (parallel, concurrency={getattr(config, 'PARALLEL_HISTORICAL_CONCURRENCY', 12)})...")
    metrics = fetch_daily_metrics_parallel(ib, to_scan, max_tickers=len(to_scan))
    # Rank by candidate score
    scored = [(sym, _candidate_score(m)) for sym, m in metrics.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    watchlist = [s for s, _ in scored[:top_n]]
    # Return metrics only for watchlist so daily_metrics has all we need for real-time
    watch_metrics = {s: metrics[s] for s in watchlist if s in metrics}
    print(f"  Got {len(metrics)} candidates, watchlist top {len(watchlist)}; top 5: {', '.join(watchlist[:5])}")
    return watchlist, watch_metrics
