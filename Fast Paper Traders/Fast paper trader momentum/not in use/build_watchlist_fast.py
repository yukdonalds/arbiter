# -----------------------------
# Fast Paper Trader – Pre-build watchlist and daily metrics cache
# Run this once (e.g. before market open or overnight) so main.py starts in seconds.
# -----------------------------
"""
Run: python build_watchlist_fast.py
Connects to IB, fetches daily data in parallel for FAST_SCAN_MAX_TICKERS,
saves watchlist and metrics to data/. Next run of main.py will use cache (instant).
"""
import config
from ib_connection import connect_ib, disconnect_ib
from parallel_fetch import build_watchlist_parallel
from metrics_cache import save_cached_metrics, save_cached_watchlist


def load_tickers() -> list[str]:
    out = []
    with open(config.SP500_TICKERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.upper())
    return out


def main():
    print("Fast Paper Trader – Building watchlist cache")
    tickers = load_tickers()
    if not tickers:
        print("No tickers in sp500_tickers.txt.")
        return
    ib = connect_ib()
    ib.reqMarketDataType(getattr(config, "MARKET_DATA_TYPE", 3))
    ib.sleep(1)
    try:
        watchlist, daily_metrics = build_watchlist_parallel(ib, tickers, top_n=getattr(config, "WATCHLIST_TOP_N", 100))
        if daily_metrics:
            save_cached_metrics(daily_metrics)
            save_cached_watchlist(watchlist)
            print(f"Saved {len(daily_metrics)} metrics and {len(watchlist)} watchlist tickers to data/")
        else:
            print("No metrics obtained; cache not updated.")
    finally:
        disconnect_ib(ib)
    print("Done. Run main.py for fast startup using this cache.")


if __name__ == "__main__":
    main()

