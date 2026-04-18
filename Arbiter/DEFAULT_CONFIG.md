# Default config - Fast Paper Long/Short

Use this when you want to **set config back to default** (for example: "set it back to default").

These values are synced to current `config.py` defaults.

| Group | Default values |
|---|---|
| **IBKR connection** | `IB_HOST = "127.0.0.1"`, `IB_PORT = 7497`, `IB_CLIENT_ID = 1`, `MARKET_DATA_TYPE = 1` |
| **Universe and scan** | `FAST_SCAN_MAX_TICKERS = 501`, `WATCHLIST_TOP_N = 150`, `MAX_REALTIME_SUBSCRIPTIONS = 100`, `PARALLEL_HISTORICAL_CONCURRENCY = 12`, `CACHE_MAX_AGE_DAYS = 1`, `USE_EXTERNAL_SCREEN = True`, `USE_FIXED_UNIVERSE = True` |
| **Market bias** | `ETF_SYMBOL = "SPY"`, `ENABLE_SHORTS = True`, `SPY_HARD_TREND_FILTER = True`, `BIAS_REFRESH_INTERVAL = 300`, `MIN_BIAS_STRENGTH = 0.30`, `BIAS_EMA_FAST = 20`, `BIAS_EMA_SLOW = 50`, `BIAS_SLOPE_LOOKBACK = 5`, `BIAS_NEUTRAL_FALLBACK = "LONG"`, `ALLOW_TRADES_IN_NEUTRAL = True` |
| **Signal filters** | `PRICE_MIN = 5.0`, `PRICE_MAX = 200.0`, `MIN_PCT_CHANGE_1D = 0.5`, `MIN_RELATIVE_VOLUME = 0.6`, `ATR_PERIOD = 14`, `ATR_PCT_MIN = 0.5`, `ATR_PCT_MAX = 10.0`, `MAX_DISTANCE_FROM_VWAP_PCT = 0.5`, `MIN_AVG_DAILY_VOLUME = 800000`, `BACKTEST_MIN_AVG_DAILY_VOLUME = 300000`, `LIQUIDITY_VOLUME_SPIKE_MIN = 0.8`, `VOLUME_LOOKBACK = 20`, `SCORE_WEIGHTS = (2.0, 10.0, 1.0)`, `MIN_SIGNALS_TO_TRADE = 1` |
| **Risk and position limits** | `MAX_SIGNALS_PER_DAY = 20`, `MAX_POSITION_PCT = 0.15`, `MAX_POSITIONS = 10`, `MAX_CAPITAL_PCT_USED = 0.98`, `MAX_DAILY_LOSS_PCT = 0.05`, `ENTRY_ORDER_TIMEOUT_SECONDS = 900` |
| **Exits** | `TARGET_PCT = 0.04`, `STOP_PCT = 0.03`, `TRAIL_BREAKEVEN_MFE_PCT = 1.5`, `TRAIL_ACTIVATE_MFE_PCT = 2.5`, `TRAIL_DISTANCE_PCT = 1.5`, `PARTIAL_TP_PCT = 0.025`, `PARTIAL_TP_FRACTION = 0.70`, `RUNNER_CAP_PCT = 0.06` |
| **Session and bars** | `MARKET_OPEN_HOUR = 9`, `MARKET_OPEN_MINUTE = 45`, `STOP_SIGNALS_HOUR = 15`, `STOP_SIGNALS_MINUTE = 55`, `CLOSE_POSITIONS_HOUR = 15`, `CLOSE_POSITIONS_MINUTE = 45`, `SHUTDOWN_HOUR = 16`, `SHUTDOWN_MINUTE = 0`, `BAR_MINUTES = 2`, `INTRABAR_MIN_AGE_SECONDS = 30`, `DISPLAY_TIMEZONE = "Australia/Sydney"` |
| **Safety and debug** | `KILL_SWITCH_FILE = os.path.join(BASE_DIR, "KILL_SWITCH.txt")`, `DEBUG_SIGNAL_CONDITIONS = True`, `DEBUG_RELAX_FILTERS = False` |

When the AI rule says "set back to default", use this file as the canonical baseline.
