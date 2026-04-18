# -----------------------------
# Fast Paper Trader – Config
# Paths and params; same logic as mission, tuned for speed (cache + parallel).
# -----------------------------
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

# v2.6 signal – relaxed to capture more opportunities (see REVERT below to restore strict)
PRICE_MIN, PRICE_MAX = 5.0, 200.0
MIN_PCT_CHANGE_1D = 1.0
MIN_RELATIVE_VOLUME = 1.1
ATR_PERIOD = 14
ATR_PCT_MIN, ATR_PCT_MAX = 1.0, 10.0
MAX_DISTANCE_FROM_VWAP_PCT = 5.0
# --- REVERT to strict (replace the 6 lines above with these):
# PRICE_MIN, PRICE_MAX = 5.0, 150.0
# MIN_PCT_CHANGE_1D = 1.5
# MIN_RELATIVE_VOLUME = 1.2
# ATR_PCT_MIN, ATR_PCT_MAX = 1.5, 6.0
# MAX_DISTANCE_FROM_VWAP_PCT = 3.0
MIN_AVG_DAILY_VOLUME = 1_000_000
VOLUME_LOOKBACK = 20

SCORE_WEIGHTS = (2.0, 10.0, 1.0)
MAX_SIGNALS_PER_DAY = 20
MIN_SIGNALS_TO_TRADE = 1

MAX_POSITION_PCT = 0.15
MAX_POSITIONS = 10
MAX_CAPITAL_PCT_USED = 0.98
# Cancel unfilled entry orders after this many seconds; ticker can be tried again (bumped down)
ENTRY_ORDER_TIMEOUT_SECONDS = 900  # 15 min

TARGET_PCT = 0.04
STOP_PCT = 0.03
# Trailing stop: move to breakeven when MFE >= this %; then trail below high by TRAIL_DISTANCE_PCT
TRAIL_BREAKEVEN_MFE_PCT = 2.0
TRAIL_ACTIVATE_MFE_PCT = 3.0
TRAIL_DISTANCE_PCT = 1.5

MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 30
STOP_SIGNALS_HOUR, STOP_SIGNALS_MINUTE = 15, 55
CLOSE_POSITIONS_HOUR, CLOSE_POSITIONS_MINUTE = 15, 45
SHUTDOWN_HOUR, SHUTDOWN_MINUTE = 16, 0
BAR_MINUTES = 2
# Enter earlier: fire signal when current bar has built for this many seconds (and passes v26)
INTRABAR_MIN_AGE_SECONDS = 60
DISPLAY_TIMEZONE = "Australia/Sydney"

MAX_DAILY_LOSS_PCT = 0.05
KILL_SWITCH_FILE = os.path.join(BASE_DIR, "KILL_SWITCH.txt")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)
