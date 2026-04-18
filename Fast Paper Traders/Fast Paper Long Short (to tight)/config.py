"""
Fast Paper Long/Short (FPLS) – Config

Standalone fork of "Fast paper trader momentum" with:
- ETF market-bias (LONG/SHORT/NEUTRAL)
- Directional entries + short execution support
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
REPORT_DIR = os.path.join(BASE_DIR, "Reports")
SESSION_LOG_FILE = os.path.join(LOG_DIR, "session.log")

# IBKR connection (paper)
IB_HOST = "127.0.0.1"
IB_PORT = 7497
IB_CLIENT_ID = 1
MARKET_DATA_TYPE = 1

# Paths
SP500_TICKERS_FILE = os.path.join(BASE_DIR, "sp500_tickers.txt")
DAILY_METRICS_CACHE = os.path.join(DATA_DIR, "daily_metrics_cache.csv")
WATCHLIST_CACHE = os.path.join(DATA_DIR, "watchlist_cache.txt")

# Fast scan: max tickers to scan when building watchlist (no cache); use full list for best universe
FAST_SCAN_MAX_TICKERS = 501
# Top N tickers to use for real-time bars (watchlist size)
WATCHLIST_TOP_N = 150
# IB cap on concurrent real-time bar streams (Error 456); paper/retail often ~100
MAX_REALTIME_SUBSCRIPTIONS = 100
# Parallel requests at once (IB rate limit friendly)
PARALLEL_HISTORICAL_CONCURRENCY = 12
# Use cache if it's from today or last trading day (no older)
CACHE_MAX_AGE_DAYS = 1

# Screen watchlist from non-IB data (Wikipedia + yfinance) for full S&P 500; fallback to IB parallel if False or on failure
USE_EXTERNAL_SCREEN = True

# --- Market bias (ETF) ---
ETF_SYMBOL = "SPY"
ENABLE_SHORTS = False
# Alias for readability in strategy tuning; live/backtest code checks both flags.
ALLOW_SHORTS = False
BIAS_REFRESH_INTERVAL = 300  # seconds
MIN_BIAS_STRENGTH = 0.30
BIAS_EMA_FAST = 20
BIAS_EMA_SLOW = 50
BIAS_SLOPE_LOOKBACK = 5

# v2.6 signal – relaxed (same as your momentum version)
PRICE_MIN, PRICE_MAX = 5.0, 200.0
MIN_PCT_CHANGE_1D = 0.5
MIN_RELATIVE_VOLUME = 0.6
ATR_PERIOD = 14
ATR_PCT_MIN, ATR_PCT_MAX = 0.5, 10.0
# Note: dist_vwap is expressed as a percent in logs (e.g. 0.03 = 0.03%).
MAX_DISTANCE_FROM_VWAP_PCT = 0.5
MIN_AVG_DAILY_VOLUME = 800_000
# Backtest-only: lower threshold so replayed 5-sec data can pass liquidity (live never uses this)
BACKTEST_MIN_AVG_DAILY_VOLUME = 300_000
# Volume spike for liquidity: today's volume (so far) >= this × avg daily volume
LIQUIDITY_VOLUME_SPIKE_MIN = 0.8
VOLUME_LOOKBACK = 20

# Strong-entry filters (used by signal_engine for both live and backtest).
# Require directional move, participation, trend quality, and tradable intrabar range.
MIN_MOVE_PCT = 1.0
MIN_VOLUME_MULTIPLIER = 2.0
MIN_TREND_SCORE = 0.65
MIN_EXPECTED_RANGE = 1.5

SCORE_WEIGHTS = (2.0, 10.0, 1.0)
MAX_SIGNALS_PER_DAY = 20
MIN_SIGNALS_TO_TRADE = 1

# Re-entry controls: allow another entry in the same ticker only after a prior winning trade.
ALLOW_REENTRY = True
REENTRY_ONLY_IF_LAST_TRADE_WIN = True
REENTRY_MAX_PER_TICKER = 2

MAX_POSITION_PCT = 0.20
MAX_POSITIONS = 12
MAX_CAPITAL_PCT_USED = 0.98
# Cancel unfilled entry orders after this many seconds; ticker can be tried again (bumped down)
ENTRY_ORDER_TIMEOUT_SECONDS = 900  # 15 min

TARGET_PCT = 0.04
STOP_PCT = 0.03

# Trailing stop (direction-aware in FPLS runner)
TRAIL_BREAKEVEN_MFE_PCT = 1.5
TRAIL_ACTIVATE_MFE_PCT = 2.5
TRAIL_DISTANCE_PCT = 1.5

# --- Exits: partial TP + runner (FPLS) ---
# Take partial profits, then manage remainder with trailing stop.
PARTIAL_TP_PCT = 0.025          # +2.5% (LONG) / -2.5% (SHORT)
PARTIAL_TP_FRACTION = 0.70      # take 70% off at TP1
RUNNER_CAP_PCT = 0.06           # cap remainder at +6% / -6% (optional "TP2")

MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 30
STOP_SIGNALS_HOUR, STOP_SIGNALS_MINUTE = 15, 55
CLOSE_POSITIONS_HOUR, CLOSE_POSITIONS_MINUTE = 15, 45
SHUTDOWN_HOUR, SHUTDOWN_MINUTE = 16, 0
BAR_MINUTES = 2
INTRABAR_MIN_AGE_SECONDS = 30
DISPLAY_TIMEZONE = "Australia/Sydney"

MAX_DAILY_LOSS_PCT = 0.05
KILL_SWITCH_FILE = os.path.join(BASE_DIR, "KILL_SWITCH.txt")

# Backtest: fixed universe = first 100 from SP500_TICKERS_FILE (with cache metrics); else cached watchlist
USE_FIXED_UNIVERSE = True

# When bias would be NEUTRAL, use this direction instead so we don't block all trades (bias, don't gate).
# Set to "LONG" or "SHORT"; None = keep NEUTRAL (strict).
BIAS_NEUTRAL_FALLBACK = "LONG"
# Allow both long and short when bias is NEUTRAL (bias = tilt, not permission).
ALLOW_TRADES_IN_NEUTRAL = True

# --- Debugging ---
# If True, print per-ticker condition booleans + key inputs as signals are evaluated.
DEBUG_SIGNAL_CONDITIONS = True
# If True, relax filters so you can confirm signal generation is working (use temporarily).
DEBUG_RELAX_FILTERS = False

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

