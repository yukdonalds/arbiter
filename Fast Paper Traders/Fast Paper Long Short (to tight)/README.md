# Fast Paper Trader

Faster startup version of the Paper/mission paper trading system. All code and data live in this folder (no shared imports from mission).

## Why it’s faster

| Step | Paper/mission | Fast paper trader |
|------|----------------|-------------------|
| Watchlist scan | ~500 tickers, **sequential** `reqHistoricalData` + 0.1s sleep each (~1 hr) | **Cache-first**: if cache is fresh, **instant**. Else **parallel** fetch for full list (501 tickers; ~10–20 min) |
| Pre-market metrics | Up to 200 tickers **sequential** | Same as watchlist: from cache or from parallel scan (no extra sequential pass) |

- **First run (or stale cache):** Connects to IB, fetches daily data for the **full list** (up to 501 tickers) in parallel (configurable concurrency), ranks and caches top **WATCHLIST_TOP_N** (150). Typical runtime **~10–20 minutes** when using IB path; external screen (Wikipedia + yfinance) is similar.
- **Next run (fresh cache):** Loads watchlist and daily metrics from `data/daily_metrics_cache.csv` and `data/watchlist_cache.txt`. **Startup in seconds.**

## Requirements

- TWS/IB Gateway running, API enabled (paper port 7497).
- For external screening (full S&P 500): `pip install pandas yfinance` (and `lxml` or `html5lib` for Wikipedia table).
- Python with `ib_insync` (same as mission). Use mission’s venv or install in this folder.

## Usage

1. **Optional – IB-qualify tickers (avoids “unrecognized” errors):**
   ```bash
   python fetch_sp500_from_ib.py
   ```
   Qualifies the seed list in `sp500_tickers.txt` via IB and overwrites the file with IB-formatted symbols. Run periodically.

2. **Optional – pre-build cache (e.g. before market open):**
   ```bash
   python build_watchlist_fast.py
   ```
   This refreshes the watchlist and metrics so the next run of `main.py` is instant.

3. **Run the paper trader:**
   ```bash
   python main.py
   ```
   - If cache is fresh → uses cache, then subscribes to 5-sec bars and runs the same v2.6 logic as mission.
   - If cache missing or stale → tries **external screen** (Wikipedia + yfinance, full S&P 500) first; if that fails, runs IB parallel scan (full list, up to FAST_SCAN_MAX_TICKERS). Saves cache then continues.

## Config (`config.py`)

- **FAST_SCAN_MAX_TICKERS** (default 501): Max tickers to scan when building watchlist (no cache). Full list for best universe.
- **WATCHLIST_TOP_N** (default 150): Watchlist size (ranked and cached). Top N from scan.
- **MAX_REALTIME_SUBSCRIPTIONS** (default 100): IB cap on real-time bar streams (Error 456); only this many of the watchlist get 5-sec bars.
- **PARALLEL_HISTORICAL_CONCURRENCY** (default 12): Concurrent historical data requests.
- **CACHE_MAX_AGE_DAYS** (default 1): Use cache if it’s from today or last trading day.
- **DAILY_METRICS_CACHE**, **WATCHLIST_CACHE**: Paths under `data/`.

Trading logic (v2.6 signals, brackets, sizing, logs) is unchanged from mission; only **selection and startup** are optimized.

## Files (all under this folder)

- `main.py` – Entry point; cache-first startup, then same real-time loop as mission.
- `config.py` – Paths and parameters; `USE_EXTERNAL_SCREEN = True` uses non-IB data for watchlist.
- `external_screen.py` – Full S&P 500 screening via Wikipedia + yfinance (no IB historical calls).
- `build_watchlist_fast.py` – Optional script to refresh cache.
- `parallel_fetch.py` – Parallel historical data fetch (async, semaphore-limited).
- `metrics_cache.py` – Load/save daily metrics and watchlist cache.
- `ib_connection.py`, `bar_builder.py`, `signal_engine.py`, `position_sizing.py`, `order_execution.py`, `trade_logger.py` – Copies from mission, use local `config`.
- `sp500_tickers.txt` – Ticker list; run `fetch_sp500_from_ib.py` to qualify via IB and replace with IB-formatted symbols (avoids “unrecognized” errors).
- `data/` – `daily_metrics_cache.csv`, `watchlist_cache.txt`.
- `logs/` – `trades.csv`, `daily_equity.csv`, `session.log` (run log: startup, watchlist source, signals, fills, shutdown).

## How quick is it?

- **With fresh cache:** A few seconds to load cache and connect to IB.
- **Without cache:** About **10–20 minutes** for full list (501) in parallel (vs ~1 hour for 500 sequential in mission). Real-time uses top 150 tickers.
