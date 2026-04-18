# -----------------------------
# Fast Paper Trader – Daily metrics cache (load/save CSV for instant startup)
# -----------------------------
import csv
import os
from datetime import datetime, timedelta
import pytz

import config

EASTERN = pytz.timezone("America/New_York")


def _get_last_trading_day() -> datetime:
    now = datetime.now(EASTERN)
    last_day = now - timedelta(days=1)
    while last_day.weekday() >= 5:
        last_day = last_day - timedelta(days=1)
    return last_day


def cache_path() -> str:
    return getattr(config, "DAILY_METRICS_CACHE", os.path.join(config.DATA_DIR, "daily_metrics_cache.csv"))


def is_cache_fresh() -> bool:
    """True if cache file exists and is from today or last trading day."""
    path = cache_path()
    if not os.path.isfile(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=EASTERN)
    now = datetime.now(EASTERN)
    last_trading = _get_last_trading_day()
    # Accept if cache is from today or from last trading day
    if mtime.date() == now.date():
        return True
    if mtime.date() == last_trading.date():
        return True
    max_days = getattr(config, "CACHE_MAX_AGE_DAYS", 1)
    age = (now - mtime).total_seconds() / 86400
    return age <= max_days


def load_cached_metrics() -> dict | None:
    """
    Load daily metrics from cache CSV. Returns dict ticker -> { avg_vol_20, atr_pct, prev_close, yesterday_close, today_volume_so_far }
    or None if file missing / invalid.
    """
    path = cache_path()
    if not os.path.isfile(path):
        return None
    metrics = {}
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                sym = (row.get("ticker") or row.get("Ticker") or "").strip().upper()
                if not sym:
                    continue
                try:
                    metrics[sym] = {
                        "avg_vol_20": float(row.get("avg_vol_20", 0)),
                        "atr_pct": float(row.get("atr_pct", 0)),
                        "prev_close": float(row.get("prev_close", 0)),
                        "yesterday_close": float(row.get("yesterday_close", 0)),
                        "today_volume_so_far": 0.0,
                    }
                except (ValueError, TypeError):
                    continue
    except Exception:
        return None
    return metrics if metrics else None


def save_cached_metrics(metrics: dict) -> None:
    """Write daily metrics to cache CSV."""
    path = cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "avg_vol_20", "atr_pct", "prev_close", "yesterday_close"])
        for sym, m in metrics.items():
            w.writerow([
                sym,
                m.get("avg_vol_20", 0),
                m.get("atr_pct", 0),
                m.get("prev_close", 0),
                m.get("yesterday_close", 0),
            ])


def load_cached_watchlist() -> list[str] | None:
    """Load watchlist from watchlist_cache.txt (one symbol per line). Returns None if missing."""
    path = getattr(config, "WATCHLIST_CACHE", os.path.join(config.DATA_DIR, "watchlist_cache.txt"))
    if not os.path.isfile(path):
        return None
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#"):
                out.append(s)
    return out if out else None


def save_cached_watchlist(tickers: list[str]) -> None:
    path = getattr(config, "WATCHLIST_CACHE", os.path.join(config.DATA_DIR, "watchlist_cache.txt"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for s in tickers:
            f.write(s + "\n")
