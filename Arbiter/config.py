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
# Second copy of live daily reports (e.g. OneDrive). Set to "" to disable.
# Uses %OneDrive% when set (typical for your Windows user); else explicit path.
_onedrive = os.environ.get("OneDrive", "").strip()
REPORT_MIRROR_DIR = (
    os.path.join(_onedrive, "Arbiter Reports")
    if _onedrive
    else r"C:\Users\Matthew\OneDrive\Arbiter Reports"
)
SESSION_LOG_FILE = os.path.join(LOG_DIR, "session.log")

# Daily report headline & return %:
# "broker" = Net Liquidation change vs prior logged close (matches IB account day-to-day).
# "trade_log" = sum of trades.csv rows only (strategy model; use for execution-quality review).
REPORT_DAILY_PNL_PRIMARY = "broker"

# IBKR connection (paper)
IB_HOST = "127.0.0.1"
IB_PORT = 7497
IB_CLIENT_ID = 1
# Set this to your exact IB account id (recommended, e.g. "DU1234567").
# Leave as "" only if managedAccounts reliably returns a single valid account.
IB_ACCOUNT = "DUP864580"
IB_CONNECT_TIMEOUT_SEC = 20
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
ENABLE_SHORTS = True
# If True, only allow entries in the current SPY bias direction.
# LONG bias -> long-only, SHORT bias -> short-only, NEUTRAL bias -> no entries.
SPY_HARD_TREND_FILTER = False
BIAS_REFRESH_INTERVAL = 300  # seconds
MIN_BIAS_STRENGTH = 0.30
BIAS_EMA_FAST = 20
BIAS_EMA_SLOW = 50
BIAS_SLOPE_LOOKBACK = 5

# --- SPY regime engine (capital multiplier only; uses ETF bars already in BarBuilder) ---
REGIME_ENGINE_ENABLED = True
# Equal weights → regime_score is mean of components in [0, 1].
REGIME_WEIGHT_TREND = 1.0
REGIME_WEIGHT_VOL = 1.0
REGIME_WEIGHT_ER = 1.0
# Aggregate bars (BAR_MINUTES): ~40 bars ≈ 80 minutes at 2m.
REGIME_TREND_LOOKBACK = 40
REGIME_ER_LOOKBACK = 40
REGIME_ATR_PERIOD = 14
REGIME_VOL_BASELINE_BARS = 64
# Map abs(N-bar return %) into [0,1]; lower = need stronger move to score as “trending”.
REGIME_TREND_SCALE_PCT = 1.25
REGIME_LOG_VERBOSE = False  # if True, one REGIME line per `process_signals` (live) when engine is on

# v2.7 signal – aggressive momentum (relaxed volatility & VWAP for explosive moves)
PRICE_MIN, PRICE_MAX = 5.0, 200.0
MIN_PCT_CHANGE_1D = 0.5
MIN_RELATIVE_VOLUME = 0.6
ATR_PERIOD = 14
ATR_PCT_MIN, ATR_PCT_MAX = 0.5, 15.0  # ATR_PCT_MAX 15% for explosive moves
# Note: dist_vwap is expressed as a percent in logs (e.g. 0.03 = 0.03%).
MAX_DISTANCE_FROM_VWAP_PCT = 3.0  # Relaxed 3% to catch high-conviction signals
MIN_AVG_DAILY_VOLUME = 800_000
# Backtest-only: lower threshold so replayed 5-sec data can pass liquidity (live never uses this)
BACKTEST_MIN_AVG_DAILY_VOLUME = 300_000
# Volume spike for liquidity: today's volume (so far) >= this × avg daily volume
LIQUIDITY_VOLUME_SPIKE_MIN = 0.8
VOLUME_LOOKBACK = 20

# --- Trade-flow tuning knobs ---
# Confirmation / breakout sensitivity
CONFIRMATION_BUFFER = 0.10
BREAKOUT_THRESHOLD = 0.015
# Momentum volume gate used by signal engine
MIN_VOLUME_MULTIPLIER = 0.9
# Keep momentum confirmation strict (high-probability setups only).
REQUIRE_MOMENTUM_CONFIRMATION = True
# Optional SL widening trigger for high-vol names
VOLATILITY_SL_THRESHOLD = 5.0
VOLATILITY_SL_BUFFER_PCT = 0.05
# Reject entries too close to obvious support/resistance (percent-units, e.g. 0.1 = 0.1%).
SUPPORT_RESISTANCE_PROXIMITY_PCT = 0.1
SUPPORT_RESISTANCE_LOOKBACK_BARS = 20

SCORE_WEIGHTS = (2.0, 10.0, 1.0)
MAX_SIGNALS_PER_DAY = 8
MIN_SIGNALS_TO_TRADE = 1

MAX_POSITION_PCT = 0.15
MAX_POSITIONS = 6
MAX_CAPITAL_PCT_USED = 0.98
# Cancel unfilled entry orders after this many seconds; ticker can be tried again (bumped down)
ENTRY_ORDER_TIMEOUT_SECONDS = 900  # 15 min

TARGET_PCT = 0.05
STOP_PCT = 0.025

# Trailing stop (direction-aware in FPLS runner)
TRAIL_BREAKEVEN_MFE_PCT = 1.5
TRAIL_ACTIVATE_MFE_PCT = 3.5
TRAIL_DISTANCE_PCT = 2.5

# --- Exits: partial TP + runner (FPLS) ---
# Take partial profits, then manage remainder with trailing stop.
PARTIAL_TP_PCT = 0.015          # +1.5% (LONG) / -1.5% (SHORT) – lock gains on drifting stocks
PARTIAL_TP_FRACTION = 0.50      # take 50% off at TP1, leave more for runner
RUNNER_CAP_PCT = 0.065          # cap remainder at +6.5% / -6.5% (optional "TP2")
# Runner "breakeven" buffer: when moving stop after TP1, use entry±0.5% so runner is net winner
RUNNER_SECURE_GAIN_BUFFER_PCT = 0.005

MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 45
# US/Eastern: no new entry orders at or after this time (matches Arbiter Launch process_signals).
ENTRY_CUTOFF_HOUR, ENTRY_CUTOFF_MINUTE = 14, 0
STOP_SIGNALS_HOUR, STOP_SIGNALS_MINUTE = 15, 55
CLOSE_POSITIONS_HOUR, CLOSE_POSITIONS_MINUTE = 15, 45
SHUTDOWN_HOUR, SHUTDOWN_MINUTE = 16, 0
BAR_MINUTES = 2
INTRABAR_MIN_AGE_SECONDS = 120
DISPLAY_TIMEZONE = "Australia/Sydney"

MAX_DAILY_LOSS_PCT = 0.05
KILL_SWITCH_FILE = os.path.join(BASE_DIR, "KILL_SWITCH.txt")

# Backtest: fixed universe = first 100 from SP500_TICKERS_FILE; False = top N by score from prior day (same rescan as Run Me FPLS when False)
USE_FIXED_UNIVERSE = False
# --- Backtest bar data (match live: IB 5-sec TRADES only) ---
# If True: intraday replay uses only IB historical 5-sec bars. No yfinance fill-in for missing
# symbols; cache files that are not ~5s-spaced (e.g. old 1m fallback) are ignored and refetched.
# Recommended for parity with live Arbiter (real-time 5-sec bars).
BACKTEST_IB_5SEC_ONLY = True
# 5-sec bar fetches: low concurrency eases IB pacing (Error 162). If timeouts are common, raise
# BACKTEST_BAR_TIMEOUT (seconds per request) and/or BACKTEST_BAR_RETRY_PASSES.
BACKTEST_BAR_CONCURRENCY = 2
BACKTEST_BAR_TIMEOUT = 60
BACKTEST_BAR_RETRY_PASSES = 3
BACKTEST_BAR_RETRY_DELAY_SEC = 1.5
# If IB cannot return 5-sec bars for a symbol: when BACKTEST_IB_5SEC_ONLY is False, optionally
# fill with yfinance (1m — not comparable to live). Ignored when BACKTEST_IB_5SEC_ONLY is True.
BACKTEST_FALLBACK_YFINANCE = False
BACKTEST_FALLBACK_YF_INTERVAL = "1m"
# Cache bars to disk; re-runs skip fetch (set False to force fresh)
BACKTEST_USE_CACHE = True
BACKTEST_BARS_CACHE_DIR = os.path.join(DATA_DIR, "backtest_bars")

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

