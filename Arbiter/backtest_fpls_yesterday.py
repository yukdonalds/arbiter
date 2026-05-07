# -----------------------------
# Fast Paper Long/Short – Backtest with 5-sec bars (IBKR)
# -----------------------------
"""
Run: python backtest_fpls_yesterday.py [--from YYYY-MM-DD] [--to YYYY-MM-DD]

- Date range: --from and --to set the backtest period (default: yesterday only).
- Universe: USE_FIXED_UNIVERSE = True → first 100 from S&P list (fixed).
  False → per-day rescan: for each trading day, build watchlist from prior day's
  data only (yfinance or IB). Top WATCHLIST_TOP_N by score. No lookahead.
- Fetches 5-sec bars from IBKR for SPY + tickers for each trading day (see config
  BACKTEST_IB_5SEC_ONLY: no yfinance intraday mix when True; cache must look like ~5s bars).
- Replays bar-by-bar: bias, SPY MA20 trend filter, 2-bar confirmation, SPY regime sizing, signals, execution.
- Entry/execution rules mirror Arbiter Launch `process_signals`: ENTRY_CUTOFF_* (default 14:00 ET),
  MARKET_OPEN_*, adaptive time sizing, loss-streak sizing, late-day bias block, ATR/score filters,
  MAX_DAILY_LOSS_PCT, subscription-sized universe (MAX_REALTIME_SUBSCRIPTIONS).
- Fill: LONG entry = bar.high + slippage, SHORT entry = bar.low - slippage (stress mode); live_parity
  uses signal close ±0.1% like live marketable limits.
- Bracket exits: partial TP + runner, stop before target in same bar.
- Writes logs/backtest_trades_YYYY-MM-DD.csv (or backtest_trades_YYYY-MM-DD_to_YYYY-MM-DD.csv).
"""

import argparse
import asyncio
import csv
import os
import pickle
import random
import statistics
import sys
import time as time_mod
from datetime import date, datetime, timedelta

import pytz
import yfinance as yf

import config
from ib_connection import connect_ib, make_stock, disconnect_ib
from bar_builder import BarBuilder
from market_bias import get_market_bias_from_closes
from signal_engine import check_v26_bar_side, rank_and_cap
from position_sizing import size_per_trade
from regime_engine import compute_regime_from_barbuilder
from metrics_cache import load_cached_metrics, load_cached_watchlist
from parallel_fetch import build_watchlist_parallel_for_date, fetch_daily_metrics_parallel_for_date
from daily_report import generate_backtest_report
from universe_rescan import rescan_universe_for_day
from stats_collector import init_trade_outcomes_log, log_trade_outcome

EASTERN = pytz.timezone("America/New_York")
BAR_MINUTES = getattr(config, "BAR_MINUTES", 2)
ETF_SYMBOL = getattr(config, "ETF_SYMBOL", "SPY").upper()
BIAS_REFRESH_INTERVAL = float(getattr(config, "BIAS_REFRESH_INTERVAL", 300))
MIN_BIAS_STRENGTH = float(getattr(config, "MIN_BIAS_STRENGTH", 0.6))
INTRABAR_MIN_AGE_SECONDS = getattr(config, "INTRABAR_MIN_AGE_SECONDS", 60)
ENTRY_SLIPPAGE_MIN = 0.01
ENTRY_SLIPPAGE_MAX = 0.05
ENTRY_SPREAD = 0.01


def _ts() -> str:
    return datetime.now(EASTERN).strftime("%H:%M:%S")


def _p(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _progress_bar(prefix: str, current: int, total: int, bar_width: int = 25) -> None:
    """Print an in-place progress bar. Call with current==total to finish and newline."""
    if total <= 0:
        return
    pct = current / total
    filled = min(int(bar_width * pct), bar_width)
    bar = "=" * filled + (">" if filled < bar_width else "") + " " * (bar_width - filled - 1)
    sys.stdout.write(f"\r  {prefix} [{bar}] {current}/{total}  ")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _yesterday_et() -> datetime:
    """Last trading day, 16:00 ET (market close)."""
    # Use local system date for default "yesterday" selection.
    now = datetime.now()
    d = now.date()
    for _ in range(1, 8):
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            break
    return EASTERN.localize(datetime.combine(d, datetime.min.time().replace(hour=16, minute=0, second=0, microsecond=0)))


def _end_dt_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d %H:%M:%S US/Eastern")


def _minutes_since_market_open(ts_et: datetime) -> int:
    """Minutes since regular-session open (default 09:30 ET)."""
    open_h = int(getattr(config, "RTH_OPEN_HOUR", 9))
    open_m = int(getattr(config, "RTH_OPEN_MINUTE", 30))
    market_open = ts_et.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    return int(max(0, (ts_et - market_open).total_seconds() // 60))


def _compute_adx_for_bars(bars: list[dict], period: int = 14) -> float:
    """
    Compute ADX (Wilder) from a list of OHLC bars.
    Returns 0.0 if insufficient history.
    """
    if not bars or len(bars) < (2 * period + 1):
        return 0.0

    sub = bars[-(2 * period + 1):]
    highs = [float(b.get("high") or 0.0) for b in sub]
    lows = [float(b.get("low") or 0.0) for b in sub]
    closes = [float(b.get("close") or 0.0) for b in sub]

    tr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(sub)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr_i = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr.append(float(tr_i))
        plus_dm.append(float(pdm))
        minus_dm.append(float(mdm))

    if len(tr) < period + 1:
        return 0.0

    sm_tr = sum(tr[:period])
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])
    if sm_tr == 0:
        return 0.0

    def _dx(di_plus: float, di_minus: float) -> float:
        denom = di_plus + di_minus
        if denom == 0:
            return 0.0
        return 100.0 * abs(di_plus - di_minus) / denom

    dxs: list[float] = []
    for i in range(period - 1, len(tr)):
        if i != period - 1:
            sm_tr = sm_tr - (sm_tr / period) + tr[i]
            sm_plus = sm_plus - (sm_plus / period) + plus_dm[i]
            sm_minus = sm_minus - (sm_minus / period) + minus_dm[i]

        if sm_tr == 0:
            di_plus = 0.0
            di_minus = 0.0
        else:
            di_plus = 100.0 * (sm_plus / sm_tr)
            di_minus = 100.0 * (sm_minus / sm_tr)
        dxs.append(_dx(di_plus, di_minus))

    if len(dxs) < period:
        return 0.0

    adx = sum(dxs[:period]) / period
    for k in range(period, len(dxs)):
        adx = ((adx * (period - 1)) + dxs[k]) / period
    return float(adx)


def fetch_5sec_bars_one(ib, symbol: str, end_dt_str: str) -> list[dict]:
    """Return list of {date, open, high, low, close, volume} for 5-sec bars, RTH."""
    timeout = int(getattr(config, "BACKTEST_BAR_TIMEOUT", 60))
    try:
        contract = make_stock(symbol)
        bars = ib.reqHistoricalData(
            contract, end_dt_str, "1 D", "5 secs", "TRADES",
            useRTH=True, timeout=timeout, formatDate=1
        )
    except Exception as e:
        print(f"  {symbol}: {e}")
        return []
    out = []
    for b in bars:
        t = b.date if hasattr(b.date, "tzinfo") and getattr(b.date, "tzinfo") else b.date
        if t.tzinfo is None:
            t = EASTERN.localize(t)
        out.append({
            "date": t,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": int(b.volume),
        })
    return out


def _bars_to_list(bars) -> list[dict]:
    """Convert IB bars to list of dicts (same format as fetch_5sec_bars_one)."""
    out = []
    for b in bars or []:
        t = b.date if hasattr(b.date, "tzinfo") and getattr(b.date, "tzinfo") else b.date
        if t.tzinfo is None:
            t = EASTERN.localize(t)
        out.append({
            "date": t,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": int(b.volume),
        })
    return out


def _median_bar_gap_seconds(bars: list[dict], max_pairs: int = 500) -> float | None:
    """Median seconds between consecutive bar timestamps (detect 1m yfinance vs ~5s IB)."""
    if not bars or len(bars) < 3:
        return None
    gaps: list[float] = []
    n = min(len(bars), max_pairs + 1)
    for i in range(1, n):
        t0, t1 = bars[i - 1].get("date"), bars[i].get("date")
        if t0 is None or t1 is None:
            continue
        try:
            gaps.append(abs((t1 - t0).total_seconds()))
        except Exception:
            continue
    if len(gaps) < 5:
        return None
    return float(statistics.median(gaps))


def bars_look_like_ib_5sec_TRADES(bars: list[dict]) -> bool:
    """
    True if bar spacing matches IB 5-sec TRADES history. Rejects yfinance 1m (median gap ~60s)
    and empty/invalid series so stale caches are refetched when BACKTEST_IB_5SEC_ONLY.
    """
    med = _median_bar_gap_seconds(bars)
    if med is None:
        return False
    return 2.0 <= med <= 20.0


def _load_cached_bars(date_str: str, symbol: str) -> list[dict] | None:
    """Load bars from cache if present; under BACKTEST_IB_5SEC_ONLY, reject non-5s caches."""
    if not getattr(config, "BACKTEST_USE_CACHE", True):
        return None
    cache_dir = getattr(config, "BACKTEST_BARS_CACHE_DIR", os.path.join(config.DATA_DIR, "backtest_bars"))
    path = os.path.join(cache_dir, date_str, f"{symbol}.pkl")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            bars = pickle.load(f)
    except Exception:
        return None
    if getattr(config, "BACKTEST_IB_5SEC_ONLY", False) and bars:
        if not bars_look_like_ib_5sec_TRADES(bars):
            return None
    return bars


def _save_cached_bars(date_str: str, symbol: str, bars: list[dict]) -> None:
    """Save 5-sec bars to cache."""
    if not getattr(config, "BACKTEST_USE_CACHE", True):
        return
    cache_dir = getattr(config, "BACKTEST_BARS_CACHE_DIR", os.path.join(config.DATA_DIR, "backtest_bars"))
    day_dir = os.path.join(cache_dir, date_str)
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, f"{symbol}.pkl")
    try:
        with open(path, "wb") as f:
            pickle.dump(bars, f)
    except Exception:
        pass


def _fetch_intraday_bars_yf(symbol: str, day: date) -> list[dict]:
    """
    Fallback intraday bars from yfinance when IB data is unavailable.
    Returns list of bar dicts compatible with the backtest stream.
    """
    interval = str(getattr(config, "BACKTEST_FALLBACK_YF_INTERVAL", "1m") or "1m")
    try:
        start = day.strftime("%Y-%m-%d")
        end = (day + timedelta(days=1)).strftime("%Y-%m-%d")
        df = yf.download(
            tickers=symbol,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
            prepost=False,
            group_by="column",
        )
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []
    try:
        # yfinance may return MultiIndex columns for single-ticker requests on some versions.
        if hasattr(df, "columns") and getattr(df.columns, "nlevels", 1) > 1:
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        if getattr(df.index, "tz", None) is None:
            df.index = df.index.tz_localize("UTC").tz_convert(EASTERN)
        else:
            df.index = df.index.tz_convert(EASTERN)
        df = df.between_time("09:30", "16:00")
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []
    out = []
    for ts, row in df.iterrows():
        try:
            # Use second=5 so minute-boundary processing path still runs.
            t = ts.to_pydatetime().replace(second=5, microsecond=0)
            out.append(
                {
                    "date": t,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row.get("Volume", 0) or 0),
                }
            )
        except Exception:
            continue
    return out


async def _fetch_5sec_bars_one_async(
    ib, sem, symbol: str, end_dt_str: str, timeout_sec: int
) -> tuple[str, list[dict]]:
    """Fetch 5-sec bars for one symbol (async). Errors return empty list (no per-symbol spam)."""
    async with sem:
        try:
            contract = make_stock(symbol)
            bars = await ib.reqHistoricalDataAsync(
                contract, end_dt_str, "1 D", "5 secs", "TRADES",
                useRTH=True, timeout=timeout_sec, formatDate=1
            )
            return symbol, _bars_to_list(bars)
        except Exception:
            return symbol, []


def fetch_5sec_bars_parallel(ib, symbols: list[str], end_dt_str: str, date_str: str) -> dict[str, list[dict]]:
    """
    Fetch 5-sec bars using limited concurrency (2 channels), short timeout, and retry passes
    for misses after each wave completes — eases IBKR pacing / Error 162.
    """
    concurrency = int(getattr(config, "BACKTEST_BAR_CONCURRENCY", 2))
    timeout_sec = int(getattr(config, "BACKTEST_BAR_TIMEOUT", 60))
    max_passes = max(1, int(getattr(config, "BACKTEST_BAR_RETRY_PASSES", 3)))
    retry_delay = float(getattr(config, "BACKTEST_BAR_RETRY_DELAY_SEC", 1.5))
    result = {}

    # Check cache first
    to_fetch = []
    for sym in symbols:
        cached = _load_cached_bars(date_str, sym)
        if cached is not None:
            result[sym] = cached
        else:
            to_fetch.append(sym)

    if not to_fetch:
        return result

    n_total = len(to_fetch)
    remaining = list(to_fetch)

    async def run_pass(pass_symbols: list[str], pass_label: str):
        if not pass_symbols:
            return []
        sem = asyncio.Semaphore(concurrency)
        tasks = [
            _fetch_5sec_bars_one_async(ib, sem, sym, end_dt_str, timeout_sec)
            for sym in pass_symbols
        ]
        out = []
        done = 0
        n_pass = len(pass_symbols)
        for coro in asyncio.as_completed(tasks):
            r = await coro
            out.append(r)
            done += 1
            _progress_bar(pass_label, done, n_pass)
        return out

    async def run_all_passes():
        nonlocal remaining
        for attempt in range(max_passes):
            if not remaining:
                break
            if attempt > 0:
                await asyncio.sleep(retry_delay)
                sys.stdout.write("\n")
                sys.stdout.flush()
                _p(
                    f"  Bars retry {attempt}/{max_passes - 1} "
                    f"({len(remaining)} symbols still missing)..."
                )
            label = "Bars" if attempt == 0 else f"Retry{attempt + 1}"
            responses = await run_pass(remaining, label)
            next_remaining = []
            for r in responses:
                if isinstance(r, Exception):
                    continue
                sym, bars = r
                if bars:
                    result[sym] = bars
                    _save_cached_bars(date_str, sym, bars)
                else:
                    next_remaining.append(sym)
            remaining = next_remaining

    ib.run(run_all_passes())

    strict_ib_5s = bool(getattr(config, "BACKTEST_IB_5SEC_ONLY", False))
    if remaining:
        _p(f"  No bars after {max_passes} pass(es): {len(remaining)} symbols (e.g. {', '.join(remaining[:8])})")
        allow_yf = bool(getattr(config, "BACKTEST_FALLBACK_YFINANCE", False)) and not strict_ib_5s
        if strict_ib_5s:
            _p("  BACKTEST_IB_5SEC_ONLY=True: yfinance disabled — missing symbols excluded from replay.")
        elif allow_yf:
            _p(f"  Fallback: yfinance intraday for {len(remaining)} symbols...")
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                day = datetime.now(EASTERN).date()
            fetched = 0
            total = len(remaining)
            for i, sym in enumerate(list(remaining), 1):
                bars = _fetch_intraday_bars_yf(sym, day)
                if bars:
                    result[sym] = bars
                    _save_cached_bars(date_str, sym, bars)
                    fetched += 1
                _progress_bar("YF", i, total)
            _p(f"  Fallback complete: {fetched}/{total} symbols")
    return result


def fetch_daily_metrics_one(ib, symbol: str, end_dt_str: str) -> dict | None:
    """
    Compute daily metrics using daily bars ending at end_dt_str (should be PRIOR trading day close).
    Returns dict: { avg_vol_20, atr_pct, prev_close, yesterday_close, today_volume_so_far }.
    """
    try:
        contract = make_stock(symbol)
        # Fetch enough daily bars to compute ATR and avg vol.
        bars = ib.reqHistoricalData(
            contract,
            end_dt_str,
            "90 D",
            "1 day",
            "TRADES",
            useRTH=True,
            timeout=60,
            formatDate=1,
        )
    except Exception:
        return None

    if not bars or len(bars) < 2:
        return None

    atr_period = int(getattr(config, "ATR_PERIOD", 14))
    vol_lookback = int(getattr(config, "VOLUME_LOOKBACK", 20))
    atr_period = max(1, atr_period)
    vol_lookback = max(1, vol_lookback)

    # Convert to floats
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]
    vols = [float(b.volume) for b in bars]

    yesterday_close = closes[-1]
    prev_close = closes[-2]

    # Avg vol over last N daily bars (including the last bar, which is yesterday).
    n = min(vol_lookback, len(vols))
    avg_vol_20 = sum(vols[-n:]) / n if n > 0 else 0.0

    # ATR% over last atr_period (TR computed using prior close)
    if len(closes) < atr_period + 1:
        atr_pct = 0.0
    else:
        tr_list = []
        # Use last atr_period bars (ending at yesterday)
        for j in range(atr_period):
            i = -(j + 1)
            h = highs[i]
            l = lows[i]
            prev_c = closes[i - 1]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_list.append(tr)
        atr = sum(tr_list) / len(tr_list) if tr_list else 0.0
        atr_pct = (atr / yesterday_close * 100.0) if yesterday_close else 0.0

    return {
        "avg_vol_20": float(avg_vol_20),
        "atr_pct": float(atr_pct),
        "prev_close": float(prev_close),
        "yesterday_close": float(yesterday_close),
        "today_volume_so_far": 0.0,
    }


def _prior_trading_day_close_et(backtest_day_close_et: datetime) -> datetime:
    """Return prior weekday 16:00 ET for daily-metrics anchoring (no lookahead)."""
    d = backtest_day_close_et.date()
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return EASTERN.localize(datetime.combine(d, datetime.min.time().replace(hour=16, minute=0, second=0, microsecond=0)))


def _compute_spy_trend_from_barbuilder(bar_builder, etf_symbol: str) -> dict:
    """
    SPY trend gate: LONG when price > MA20 by >0.2%, SHORT when < MA20 by >0.2%, NEUTRAL within ±0.2%.
    """
    closed = bar_builder.get_all_closed(etf_symbol)
    closes = [float(b.get("close") or 0) for b in closed if (b.get("close") or 0) > 0]
    cur = bar_builder.get_current_bar(etf_symbol)
    if cur and (cur.get("close") or 0) > 0:
        closes.append(float(cur["close"]))
    if len(closes) < 20:
        return {"direction": "NEUTRAL", "price": float(closes[-1]) if closes else 0.0, "ma20": 0.0, "diff_pct": 0.0}
    price = float(closes[-1])
    ma20 = float(sum(closes[-20:]) / 20.0)
    if ma20 <= 0:
        return {"direction": "NEUTRAL", "price": price, "ma20": ma20, "diff_pct": 0.0}
    diff_pct = (price - ma20) / ma20 * 100.0
    if abs(diff_pct) <= 0.2:
        direction = "NEUTRAL"
    else:
        direction = "LONG" if diff_pct > 0 else "SHORT"
    return {"direction": direction, "price": price, "ma20": ma20, "diff_pct": float(diff_pct)}


def _trading_days_between(date_from: date, date_to: date) -> list[date]:
    """Return list of weekdays from date_from through date_to (inclusive)."""
    out = []
    d = date_from
    while d <= date_to:
        if d.weekday() < 5:
            out.append(d)
        d = d + timedelta(days=1)
    return out


def load_watchlist_100() -> tuple[list[str], dict]:
    """Cached watchlist and metrics; return (tickers[:N], daily_metrics). N = WATCHLIST_TOP_N (150)."""
    watchlist = load_cached_watchlist()
    metrics = load_cached_metrics()
    if not watchlist or not metrics:
        raise SystemExit("No cached watchlist or metrics. Run live FPLS once to populate cache.")
    top_n = int(getattr(config, "WATCHLIST_TOP_N", 100))
    tickers = [s for s in watchlist if s in metrics][:top_n]
    if len(tickers) < 10:
        raise SystemExit("Fewer than 10 tickers in cache; need more for backtest.")
    dm = {s: dict(metrics[s]) for s in tickers if s in metrics}
    return tickers, dm


def load_fixed_universe_100() -> list[str]:
    """Fixed S&P subset: first 100 from sp500_tickers.txt (no cache filtering)."""
    tickers = _load_sp500_tickers(limit=100)
    return tickers


def load_all_sp500_tickers() -> list[str]:
    """Full S&P list from sp500_tickers.txt for per-day universe rescan."""
    return _load_sp500_tickers(limit=None)


def _load_sp500_tickers(limit: int | None = None) -> list[str]:
    """Load tickers from SP500_TICKERS_FILE; limit=None means all."""
    path = getattr(config, "SP500_TICKERS_FILE", os.path.join(config.BASE_DIR, "sp500_tickers.txt"))
    if not os.path.isfile(path):
        raise SystemExit("SP500_TICKERS_FILE not found.")
    tickers = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#"):
                tickers.append(s)
    if limit is not None:
        tickers = tickers[:limit]
    return tickers


def build_event_stream(etf_bars: list[dict], ticker_bars: dict[str, list[dict]]) -> list[tuple]:
    """Global event stream: (t_et, symbol, open, high, low, close, volume), sorted by t."""
    events = []
    for sym, bars in ticker_bars.items():
        for b in bars:
            events.append((b["date"], sym, b["open"], b["high"], b["low"], b["close"], b["volume"]))
    for b in etf_bars:
        events.append((b["date"], ETF_SYMBOL, b["open"], b["high"], b["low"], b["close"], b["volume"]))
    events.sort(key=lambda x: (x[0], x[1]))
    return events


def run_backtest(
    events: list[tuple],
    daily_metrics: dict,
    start_capital: float,
    date_str: str,
) -> tuple[list[dict], float, float]:
    """
    Replay events; return (list of trade dicts, end_capital, total_pnl).
    Bracket: stop checked before target in same bar (conservative).
    """
    bar_builder = BarBuilder()
    positions_today = set()
    pending_trades = {}
    signals_this_bar = []
    pending_confirmations = {}
    pending_fast_track_setups = {}
    last_closed_bar_key = {}
    last_intrabar_signal_key = {}
    capital = start_capital
    trades_out = []
    n_filled = 0
    bias_state = {"direction": "NEUTRAL", "strength": 0.0}
    last_bias_ts = 0.0
    bias_counts = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
    signal_hits_long = 0
    signal_hits_short = 0
    condition_counts = {"liquidity": 0, "price": 0, "momentum": 0, "volatility": 0, "structural": 0}
    confirmation_counts = {"strict": 0, "fast_track": 0}
    spy_soft_penalties = 0
    last_regime_snapshot: dict = {}
    session_start_capital = float(start_capital)
    trades_filled_today = 0
    loss_count_today = 0
    tickers_cancelled_today: set[str] = set()
    ticker_placed_today: set[str] = set()
    controlled_entry_block_count = 0
    same_ticker_reentry_block_count = 0
    post_confirmation_quality_block_count = 0
    entry_strength_block_count = 0
    fast_track_trade_block_count = 0
    late_entry_block_count = 0
    fast_track_setups_stored_count = 0
    fast_track_setups_confirmed_count = 0
    fast_track_setups_expired_count = 0
    close_hour = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_minute = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    eod_done = False
    last_close: dict[str, float] = {}
    fill_model = str(getattr(config, "BACKTEST_FILL_MODEL", "live_parity") or "live_parity").strip().lower()
    blocked_candidates_path = os.path.join(config.LOG_DIR, "blocked_candidates.csv")

    # Partial TP + runner settings (match live FPLS)
    TP1_PCT = float(getattr(config, "PARTIAL_TP_PCT", 0.025))
    TP1_FRACTION = float(getattr(config, "PARTIAL_TP_FRACTION", 0.70))
    RUN_CAP_PCT = float(getattr(config, "RUNNER_CAP_PCT", 0.06))
    TRAIL_BE_MFE = float(getattr(config, "TRAIL_BREAKEVEN_MFE_PCT", 1.5))
    TRAIL_ACT_MFE = float(getattr(config, "TRAIL_ACTIVATE_MFE_PCT", 2.5))
    TRAIL_DIST_PCT = float(getattr(config, "TRAIL_DISTANCE_PCT", 1.5))

    def _is_bias_tradeable(b):
        """When SPY_HARD_TREND_FILTER is on, NEUTRAL bias means no new entries."""
        d = (b.get("direction") or "NEUTRAL").upper()
        if bool(getattr(config, "SPY_HARD_TREND_FILTER", False)) and d == "NEUTRAL":
            return False
        if d == "NEUTRAL":
            return getattr(config, "ALLOW_TRADES_IN_NEUTRAL", True)
        if d == "SHORT" and not getattr(config, "ENABLE_SHORTS", True):
            return False
        return True

    def _bias_weight(signal_direction: str, bias: dict) -> float:
        """Soft bias: prefer bias-aligned (1.2) vs counter (0.8); NEUTRAL = 1.0."""
        d = (bias.get("direction") or "NEUTRAL").upper()
        if d == "LONG":
            return 1.2 if signal_direction == "LONG" else 0.8
        if d == "SHORT":
            return 1.2 if signal_direction == "SHORT" else 0.8
        return 1.0

    def _sides_from_bias(bias_dir: str) -> list[str]:
        hard_filter = bool(getattr(config, "SPY_HARD_TREND_FILTER", False))
        if not hard_filter:
            return ["LONG", "SHORT"] if bias_dir == "NEUTRAL" else [bias_dir]
        if bias_dir == "LONG":
            return ["LONG"]
        if bias_dir == "SHORT":
            return ["SHORT"] if getattr(config, "ENABLE_SHORTS", True) else []
        return []

    def _closed_bars_for_controlled(sym: str, bar: dict) -> list[dict]:
        L = bar_builder.get_all_closed(sym)
        if not bar:
            return list(L)
        if not L:
            return [dict(bar)]
        bc = float(bar.get("close") or 0)
        lc = float(L[-1].get("close") or 0)
        bh = float(bar.get("high") or bc)
        lh = float(L[-1].get("high") or lc)
        if abs(bc - lc) < 1e-4 and abs(bh - lh) < 1e-3:
            return list(L)
        return list(L) + [dict(bar)]

    def _is_controlled_entry(sym: str, side: str, close_price: float, signal_bar: dict) -> bool:
        side_u = (side or "").upper()
        c = float(close_price or 0.0)
        if c <= 0:
            return False
        h = float((signal_bar or {}).get("high") or c)
        l_ = float((signal_bar or {}).get("low") or c)
        # Rolling proxy VWAP: volume-weighted typical price over recent bars + current signal bar.
        seq = _closed_bars_for_controlled(sym, signal_bar)
        lookback = int(getattr(config, "CONTROLLED_ENTRY_PULLBACK_LOOKBACK_BARS", 15) or 15)
        vw_window = seq[-max(1, min(lookback, len(seq))):] if seq else [signal_bar or {}]
        num = 0.0
        den = 0.0
        for b in vw_window:
            bc = float(b.get("close") or 0.0)
            bh = float(b.get("high") or bc)
            bl = float(b.get("low") or bc)
            vol = float(b.get("volume") or 0.0)
            tp = (bh + bl + bc) / 3.0 if (bh or bl or bc) else 0.0
            w = vol if vol > 0 else 1.0
            num += tp * w
            den += w
        vwap = (num / den) if den > 0 else 0.0
        if vwap <= 0:
            return False
        dist_vwap_pct = (c - vwap) / vwap * 100.0

        max_ext = float(getattr(config, "MAX_DISTANCE_FROM_VWAP_PCT", 0.5) or 0.5)
        near_vwap_pct = float(getattr(config, "CONTROLLED_ENTRY_NEAR_VWAP_PCT", 0.25) or 0.25)
        pullback_pct = float(getattr(config, "CONTROLLED_ENTRY_MIN_PULLBACK_PCT", 0.30) or 0.30)
        lookback = int(getattr(config, "CONTROLLED_ENTRY_PULLBACK_LOOKBACK_BARS", 15) or 15)

        if side_u == "LONG" and dist_vwap_pct > max_ext:
            print(
                f"[ENTRY CHECK] {sym} {side_u} close={c:.4f} vwap={vwap:.4f} "
                f"recent_high=n/a recent_low=n/a dist_vwap_pct={dist_vwap_pct:.4f} -> FAIL(ext)",
                flush=True,
            )
            return False
        if side_u == "SHORT" and dist_vwap_pct < -max_ext:
            print(
                f"[ENTRY CHECK] {sym} {side_u} close={c:.4f} vwap={vwap:.4f} "
                f"recent_high=n/a recent_low=n/a dist_vwap_pct={dist_vwap_pct:.4f} -> FAIL(ext)",
                flush=True,
            )
            return False

        near_vwap = abs(dist_vwap_pct) <= near_vwap_pct
        prior = seq[:-1] if len(seq) >= 2 else seq
        if not prior:
            return near_vwap
        window = prior[-max(1, min(lookback, len(prior))):]

        if side_u == "LONG":
            recent_high = max(float(b.get("high") or 0.0) for b in window)
            pullback_from_high_pct = ((recent_high - c) / recent_high * 100.0) if recent_high > 0 else 0.0
            ok = near_vwap or (pullback_from_high_pct >= pullback_pct)
            print(
                f"[ENTRY CHECK] {sym} {side_u} close={c:.4f} vwap={vwap:.4f} "
                f"recent_high={recent_high:.4f} recent_low=n/a dist_vwap_pct={dist_vwap_pct:.4f} "
                f"pullback_pct={pullback_from_high_pct:.4f} near_vwap={near_vwap} -> {'PASS' if ok else 'FAIL'}",
                flush=True,
            )
            return ok
        if side_u == "SHORT":
            lows = [float(b.get("low") or 0.0) for b in window if float(b.get("low") or 0.0) > 0]
            if not lows:
                return near_vwap
            recent_low = min(lows)
            bounce_from_low_pct = ((c - recent_low) / recent_low * 100.0) if recent_low > 0 else 0.0
            ok = near_vwap or (bounce_from_low_pct >= pullback_pct)
            print(
                f"[ENTRY CHECK] {sym} {side_u} close={c:.4f} vwap={vwap:.4f} "
                f"recent_high=n/a recent_low={recent_low:.4f} dist_vwap_pct={dist_vwap_pct:.4f} "
                f"bounce_pct={bounce_from_low_pct:.4f} near_vwap={near_vwap} -> {'PASS' if ok else 'FAIL'}",
                flush=True,
            )
            return ok
        return False

    def _queue_for_confirmation(sig: dict, signal_bar_key: tuple) -> None:
        side = (sig.get("side") or "LONG").upper()
        signal_level = float((sig.get("bar") or {}).get("close") or 0.0)
        atr_pct = float(sig.get("atr_pct") or 0.0)
        if signal_level <= 0:
            return
        atr_abs = signal_level * max(atr_pct, 0.0) / 100.0
        confirmation_buffer = float(getattr(config, "CONFIRMATION_BUFFER", 0.25) or 0.25)
        breakout_threshold = float(getattr(config, "BREAKOUT_THRESHOLD", 0.025) or 0.025)
        buffer_abs = max(0.0, (confirmation_buffer + breakout_threshold) * atr_abs)
        confirm_level = signal_level + buffer_abs if side == "LONG" else signal_level - buffer_abs
        item = {
            "signal": sig,
            "side": side,
            "signal_level": signal_level,
            "confirm_level": float(confirm_level),
            "created_bar_key": signal_bar_key,
            "bars_checked": 0,
        }
        pending_confirmations.setdefault(sig["ticker"], []).append(item)

    def _process_confirmations_on_bar_close(sym: str, closed_bar: dict, closed_bar_key: tuple) -> None:
        nonlocal fast_track_trade_block_count, fast_track_setups_stored_count, fast_track_setups_confirmed_count, fast_track_setups_expired_count
        close_px = float(closed_bar.get("close") or 0.0)
        if close_px <= 0:
            return
        confirmation_min_move_pct = float(getattr(config, "CONFIRMATION_MIN_MOVE_PCT", 0.0) or 0.0)
        setup_confirm_bars = int(getattr(config, "FAST_TRACK_SETUP_CONFIRM_BARS", 2) or 2)
        setups = pending_fast_track_setups.get(sym) or []
        if setups:
            keep_setups = []
            for st in setups:
                if tuple(st.get("created_bar_key") or ()) == closed_bar_key:
                    keep_setups.append(st)
                    continue
                side = (st.get("side") or "LONG").upper()
                st["bars_waited"] = int(st.get("bars_waited") or 0) + 1
                confirm_level = float(st.get("confirm_level") or 0.0)
                if side == "LONG":
                    confirm_move_ok = (
                        confirm_level > 0
                        and ((close_px - confirm_level) / confirm_level) >= confirmation_min_move_pct
                    )
                    strict_confirmed = close_px >= confirm_level and confirm_move_ok
                else:
                    confirm_move_ok = (
                        confirm_level > 0
                        and ((confirm_level - close_px) / confirm_level) >= confirmation_min_move_pct
                    )
                    strict_confirmed = close_px <= confirm_level and confirm_move_ok
                if strict_confirmed:
                    sig = dict(st.get("signal") or {})
                    sig["confirmation_type"] = "strict"
                    sig["confirmed_bars"] = st["bars_waited"]
                    signals_this_bar.append(sig)
                    confirmation_counts["strict"] += 1
                    fast_track_setups_confirmed_count += 1
                    continue
                if st["bars_waited"] >= setup_confirm_bars:
                    fast_track_setups_expired_count += 1
                    _append_blocked_candidate_backtest(
                        datetime.now(EASTERN),
                        dict(st.get("signal") or {}),
                        "fast_track_setup_expired",
                    )
                    continue
                keep_setups.append(st)
            if keep_setups:
                pending_fast_track_setups[sym] = keep_setups
            else:
                pending_fast_track_setups.pop(sym, None)

        queue = pending_confirmations.get(sym)
        if not queue:
            return
        keep = []
        for item in queue:
            if tuple(item.get("created_bar_key") or ()) == closed_bar_key:
                keep.append(item)
                continue
            side = (item.get("side") or "LONG").upper()
            item["bars_checked"] = int(item.get("bars_checked") or 0) + 1
            confirm_level = float(item.get("confirm_level") or 0.0)
            if side == "LONG":
                confirm_move_ok = (
                    confirm_level > 0
                    and ((close_px - confirm_level) / confirm_level) >= confirmation_min_move_pct
                )
                confirmed = close_px >= confirm_level and confirm_move_ok
            else:
                confirm_move_ok = (
                    confirm_level > 0
                    and ((confirm_level - close_px) / confirm_level) >= confirmation_min_move_pct
                )
                confirmed = close_px <= confirm_level and confirm_move_ok
            sig = dict(item.get("signal") or {})
            sig_adx = float(sig.get("adx") or 0.0)
            sig_rel_vol = float(sig.get("rel_vol") or 0.0)
            fast_track_adx_min = float(getattr(config, "FAST_TRACK_ADX_MIN", 30.0) or 30.0)
            fast_track_rel_vol_min = float(getattr(config, "FAST_TRACK_REL_VOL_MIN", 1.5) or 1.5)
            fast_track_require_both = bool(getattr(config, "FAST_TRACK_REQUIRE_BOTH", True))
            adx_pass = sig_adx > fast_track_adx_min
            rel_vol_pass = sig_rel_vol > fast_track_rel_vol_min
            fast_track = (adx_pass and rel_vol_pass) if fast_track_require_both else (adx_pass or rel_vol_pass)
            if fast_track:
                print(f"[CONFIRM] FAST TRACK USED: {sym} {side} fast_track={fast_track}", flush=True)
                confirmation_counts["fast_track"] += 1
                sig = dict(item.get("signal") or {})
                if bool(getattr(config, "ALLOW_FAST_TRACK_TO_TRADE", False)):
                    sig["confirmation_type"] = "fast_track"
                    sig["confirmed_bars"] = item["bars_checked"]
                    signals_this_bar.append(sig)
                elif bool(getattr(config, "ALLOW_FAST_TRACK_AS_SETUP", True)):
                    setup_item = {
                        "signal": sig,
                        "side": side,
                        "confirm_level": float(confirm_level),
                        "created_bar_key": closed_bar_key,
                        "bars_waited": 0,
                    }
                    pending_fast_track_setups.setdefault(sym, []).append(setup_item)
                    fast_track_setups_stored_count += 1
                else:
                    fast_track_trade_block_count += 1
                    _append_blocked_candidate_backtest(
                        datetime.now(EASTERN),
                        sig,
                        "fast_track_trade_blocked",
                    )
                continue
            if confirmed:
                sig = dict(item.get("signal") or {})
                sig["confirmation_type"] = "strict"
                sig["confirmed_bars"] = item["bars_checked"]
                signals_this_bar.append(sig)
                confirmation_counts["strict"] += 1
                continue
            if item["bars_checked"] >= 2:
                continue
            keep.append(item)
        if keep:
            pending_confirmations[sym] = keep
        else:
            pending_confirmations.pop(sym, None)

    def _update_bias(t_et):
        nonlocal last_bias_ts, bias_state
        ts = t_et.timestamp()
        if ts - last_bias_ts < BIAS_REFRESH_INTERVAL:
            return
        closes = []
        for b in bar_builder.get_all_closed(ETF_SYMBOL):
            if (b.get("close") or 0) > 0:
                closes.append(float(b["close"]))
        cur = bar_builder.get_current_bar(ETF_SYMBOL)
        if cur and (cur.get("close") or 0) > 0:
            closes.append(float(cur["close"]))
        bias_state = get_market_bias_from_closes(closes)
        d = (bias_state.get("direction") or "NEUTRAL").upper()
        if d in bias_counts:
            bias_counts[d] += 1
        last_bias_ts = ts

    def _mfe_pct(side: str, entry: float, high: float, low: float) -> float:
        if entry <= 0:
            return 0.0
        if side == "SHORT":
            return (entry - low) / entry * 100.0
        return (high - entry) / entry * 100.0

    def _mae_pct(side: str, entry: float, high: float, low: float) -> float:
        if entry <= 0:
            return 0.0
        if side == "SHORT":
            return (entry - high) / entry * 100.0
        return (low - entry) / entry * 100.0

    def _trail_update(info: dict) -> None:
        """Update runner stop based on MFE (direction-aware), mimicking live runner logic."""
        side = (info.get("side") or "LONG").upper()
        ep = float(info.get("entry_price") or 0.0)
        if ep <= 0:
            return
        if float(info.get("runner_remaining") or 0.0) <= 0:
            return
        high_se = float(info.get("high_since_entry", ep))
        low_se = float(info.get("low_since_entry", ep))
        mfe = _mfe_pct(side, ep, high_se, low_se)
        current_stop = float(info.get("current_stop_price") or info.get("stop_price") or 0.0)

        if mfe >= TRAIL_BE_MFE and not info.get("stop_at_breakeven"):
            buf = float(getattr(config, "RUNNER_SECURE_GAIN_BUFFER_PCT", 0.005))
            if side == "LONG":
                secure_stop = round(ep * (1 + buf), 2)
            else:
                secure_stop = round(ep * (1 - buf), 2)
            info["current_stop_price"] = secure_stop
            info["stop_at_breakeven"] = True
            return

        if mfe >= TRAIL_ACT_MFE:
            if side == "LONG":
                new_stop = high_se * (1 - TRAIL_DIST_PCT / 100.0)
                if new_stop > current_stop and new_stop > ep:
                    info["current_stop_price"] = new_stop
            else:
                new_stop = low_se * (1 + TRAIL_DIST_PCT / 100.0)
                # For shorts, stop should move DOWN (toward profit) i.e. smaller number.
                if (current_stop <= 0 or new_stop < current_stop) and new_stop < ep:
                    info["current_stop_price"] = new_stop

    def _is_near_support_or_resistance(sym: str, close_price: float) -> bool:
        """
        Reject entries too close to rolling support/resistance extremes to
        avoid obvious chop/reversal zones.
        """
        if close_price <= 0:
            return False
        proximity_pct = float(getattr(config, "SUPPORT_RESISTANCE_PROXIMITY_PCT", 0.1) or 0.1)
        lookback = int(getattr(config, "SUPPORT_RESISTANCE_LOOKBACK_BARS", 20) or 20)
        if proximity_pct <= 0 or lookback < 2:
            return False
        closed_bars = bar_builder.get_all_closed(sym)
        if len(closed_bars) < lookback:
            return False
        window = closed_bars[-lookback:]
        highs = [float(b.get("high") or 0.0) for b in window]
        lows = [float(b.get("low") or 0.0) for b in window]
        res = max(highs) if highs else 0.0
        sup = min(lows) if lows else 0.0
        if res <= 0 or sup <= 0:
            return False
        near_res = abs((close_price - res) / res * 100.0) <= proximity_pct
        near_sup = abs((close_price - sup) / sup * 100.0) <= proximity_pct
        return bool(near_res or near_sup)

    def _emit_trade_row(
        sym: str,
        info: dict,
        qty: float,
        exit_price: float,
        exit_reason: str,
        t_et: datetime,
        target_price: float,
        stop_price: float,
    ) -> None:
        nonlocal loss_count_today
        side = (info.get("side") or "LONG").upper()
        entry = float(info["entry_price"])
        if side == "SHORT":
            pnl_d = (entry - exit_price) * qty
        else:
            pnl_d = (exit_price - entry) * qty
        if pnl_d < 0:
            loss_count_today += 1
        trades_out.append(
            {
                "date": date_str,
                "ticker": sym,
                "side": side,
                "entry_time": info["entry_time"],
                "entry_price": entry,
                "shares": qty,
                "target": target_price,
                "stop": stop_price,
                "exit_time": t_et.strftime("%H:%M:%S"),
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "pnl_dollars": pnl_d,
                "pnl_pct": (pnl_d / (entry * qty) * 100.0) if entry * qty else 0,
                "score": float(info.get("score") or 0.0),
                "rank_position": int(info.get("rank_position") or 0),
                "rel_vol": float(info.get("rel_vol") or 0.0),
                "atr_pct": float(info.get("atr_pct") or 0.0),
                "dist_vwap_pct": float(info.get("dist_vwap_pct") or 0.0),
                "bias_dir": str(info.get("bias_dir") or "NEUTRAL").upper(),
                "bias_strength": float(info.get("bias_strength") or 0.0),
                "confirmation_type": str(info.get("confirmation_type") or "unknown"),
                "MFE_pct": _mfe_pct(
                    side,
                    entry,
                    float(info.get("high_since_entry") or entry),
                    float(info.get("low_since_entry") or entry),
                ),
                "MAE_pct": _mae_pct(
                    side,
                    entry,
                    float(info.get("high_since_entry") or entry),
                    float(info.get("low_since_entry") or entry),
                ),
                "time_in_trade_minutes": (
                    max(0.0, (t_et - info.get("entry_dt")).total_seconds() / 60.0)
                    if isinstance(info.get("entry_dt"), datetime)
                    else 0.0
                ),
            }
        )

    def _append_blocked_candidate_backtest(t_et: datetime, sig: dict, block_reason: str) -> None:
        row = {
            "Date": t_et.strftime("%Y-%m-%d"),
            "Time": t_et.strftime("%H:%M:%S"),
            "ticker": str(sig.get("ticker") or ""),
            "side": str((sig.get("side") or "LONG")).upper(),
            "block_reason": block_reason,
        }
        fields = ["Date", "Time", "ticker", "side", "block_reason"]
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            file_exists = os.path.isfile(blocked_candidates_path)
            with open(blocked_candidates_path, "a", newline="", encoding="utf-8") as bf:
                writer = csv.DictWriter(bf, fieldnames=fields)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
        except OSError:
            pass

    def _check_exits(sym: str, bar_high: float, bar_low: float, bar_close: float, t_et: datetime):
        if sym not in pending_trades:
            return
        info = pending_trades[sym]
        side = (info.get("side") or "LONG").upper()

        # Update extremes for trailing (full trade, as live does).
        ep = float(info.get("entry_price") or 0.0)
        if ep > 0:
            info["high_since_entry"] = max(float(info.get("high_since_entry", ep)), float(bar_high))
            info["low_since_entry"] = min(float(info.get("low_since_entry", ep)), float(bar_low))
        _trail_update(info)

        tp1_price = float(info.get("tp1_price") or 0.0)
        tp2_price = float(info.get("tp2_price") or 0.0)
        stop_price = float(info.get("current_stop_price") or info.get("stop_price") or 0.0)
        tp1_rem = float(info.get("tp1_remaining") or 0.0)
        runner_rem = float(info.get("runner_remaining") or 0.0)

        # --- Runner exits (OCA between stop and TP2) ---
        if runner_rem > 0 and stop_price > 0:
            if side == "LONG":
                if bar_low <= stop_price:
                    _emit_trade_row(sym, info, runner_rem, stop_price, "STP", t_et, tp2_price, stop_price)
                    runner_rem = 0.0
                elif tp2_price and bar_high >= tp2_price:
                    _emit_trade_row(sym, info, runner_rem, tp2_price, "TP", t_et, tp2_price, stop_price)
                    runner_rem = 0.0
            else:
                if bar_high >= stop_price:
                    _emit_trade_row(sym, info, runner_rem, stop_price, "STP", t_et, tp2_price, stop_price)
                    runner_rem = 0.0
                elif tp2_price and bar_low <= tp2_price:
                    _emit_trade_row(sym, info, runner_rem, tp2_price, "TP", t_et, tp2_price, stop_price)
                    runner_rem = 0.0

        # --- Time-based quality exits (after stop/target checks, before EOD) ---
        rem_qty = tp1_rem + runner_rem
        if rem_qty > 0:
            entry_dt = info.get("entry_dt")
            open_minutes = 0.0
            if isinstance(entry_dt, datetime):
                open_minutes = max(0.0, (t_et - entry_dt).total_seconds() / 60.0)
            current_price = float(bar_close or 0.0)
            early_loss_enabled = bool(getattr(config, "ENABLE_EARLY_LOSS_EXIT", True))
            early_loss_min_minutes = float(getattr(config, "EARLY_LOSS_EXIT_MINUTES", 20) or 20)
            early_loss_pct = float(getattr(config, "EARLY_LOSS_EXIT_PCT", -1.0) or -1.0)
            early_loss_require_low_mfe = bool(getattr(config, "EARLY_LOSS_EXIT_REQUIRE_LOW_MFE", False))
            early_loss_mfe_threshold = float(getattr(config, "EARLY_LOSS_EXIT_MFE_THRESHOLD", 0.5) or 0.5)
            high_se = float(info.get("high_since_entry") or ep)
            low_se = float(info.get("low_since_entry") or ep)
            mfe_pct = _mfe_pct(side, ep, high_se, low_se) if ep > 0 else 0.0
            if current_price > 0 and ep > 0:
                unrealized_pct = (
                    ((current_price - ep) / ep * 100.0)
                    if side == "LONG"
                    else ((ep - current_price) / ep * 100.0)
                )
            else:
                unrealized_pct = 0.0
            early_loss_exit = (
                early_loss_enabled
                and open_minutes >= early_loss_min_minutes
                and unrealized_pct <= early_loss_pct
                and current_price > 0
                and (not early_loss_require_low_mfe or mfe_pct < early_loss_mfe_threshold)
            )
            if early_loss_exit:
                _emit_trade_row(sym, info, rem_qty, current_price, "EARLY_LOSS_EXIT", t_et, tp2_price, stop_price)
                tp1_rem = 0.0
                runner_rem = 0.0

        rem_qty = tp1_rem + runner_rem
        if rem_qty > 0:
            entry_dt = info.get("entry_dt")
            open_minutes = 0.0
            if isinstance(entry_dt, datetime):
                open_minutes = max(0.0, (t_et - entry_dt).total_seconds() / 60.0)
            current_price = float(bar_close or 0.0)
            high_se = float(info.get("high_since_entry") or ep)
            low_se = float(info.get("low_since_entry") or ep)
            mfe_pct = _mfe_pct(side, ep, high_se, low_se) if ep > 0 else 0.0

            weakness_exit = False
            if open_minutes >= 30.0 and mfe_pct < 0.5 and current_price > 0 and ep > 0:
                if side == "LONG":
                    weakness_exit = ((current_price - ep) / ep * 100.0) <= -0.3
                else:
                    weakness_exit = ((ep - current_price) / ep * 100.0) <= -0.3
            if weakness_exit:
                _emit_trade_row(sym, info, rem_qty, current_price, "WEAKNESS_EXIT", t_et, tp2_price, stop_price)
                tp1_rem = 0.0
                runner_rem = 0.0

        rem_qty = tp1_rem + runner_rem
        if rem_qty > 0:
            entry_dt = info.get("entry_dt")
            open_minutes = 0.0
            if isinstance(entry_dt, datetime):
                open_minutes = max(0.0, (t_et - entry_dt).total_seconds() / 60.0)
            current_price = float(bar_close or 0.0)
            if current_price > 0 and ep > 0:
                unrealized_pct = (
                    ((current_price - ep) / ep * 100.0)
                    if side == "LONG"
                    else ((ep - current_price) / ep * 100.0)
                )
            else:
                unrealized_pct = 0.0
            stale_exit = open_minutes >= 90.0 and (-0.3 <= unrealized_pct <= 0.3)
            if stale_exit and current_price > 0:
                _emit_trade_row(sym, info, rem_qty, current_price, "STALE_EXIT", t_et, tp2_price, stop_price)
                tp1_rem = 0.0
                runner_rem = 0.0

        # --- TP1 exit (independent limit) ---
        if tp1_rem > 0 and tp1_price:
            if side == "LONG":
                if bar_high >= tp1_price:
                    _emit_trade_row(sym, info, tp1_rem, tp1_price, "TP1", t_et, tp1_price, stop_price)
                    tp1_rem = 0.0
            else:
                if bar_low <= tp1_price:
                    _emit_trade_row(sym, info, tp1_rem, tp1_price, "TP1", t_et, tp1_price, stop_price)
                    tp1_rem = 0.0

        info["tp1_remaining"] = tp1_rem
        info["runner_remaining"] = runner_rem
        pending_trades[sym] = info

        # If everything is flat, remove position.
        if tp1_rem <= 0 and runner_rem <= 0:
            positions_today.discard(sym)
            del pending_trades[sym]

    def _process_signals(t_et):
        """Mirror Arbiter Launch `process_signals` gates and sizing (instant fill for backtest)."""
        nonlocal capital, n_filled, spy_soft_penalties, trades_filled_today, last_regime_snapshot, controlled_entry_block_count, same_ticker_reentry_block_count, post_confirmation_quality_block_count, entry_strength_block_count, fast_track_trade_block_count, late_entry_block_count, fast_track_setups_stored_count, fast_track_setups_confirmed_count, fast_track_setups_expired_count
        if not signals_this_bar:
            return

        ec_h = int(getattr(config, "ENTRY_CUTOFF_HOUR", 14))
        ec_m = int(getattr(config, "ENTRY_CUTOFF_MINUTE", 0))
        if (t_et.hour, t_et.minute) >= (ec_h, ec_m):
            signals_this_bar.clear()
            return
        late_block_h = int(getattr(config, "BLOCK_ENTRIES_AFTER_HOUR", 13))
        late_block_m = int(getattr(config, "BLOCK_ENTRIES_AFTER_MINUTE", 0))
        if (t_et.hour, t_et.minute) >= (late_block_h, late_block_m):
            for sig in signals_this_bar:
                late_entry_block_count += 1
                _append_blocked_candidate_backtest(t_et, sig, "late_entry_blocked")
            signals_this_bar.clear()
            return

        late_day = (t_et.hour, t_et.minute) >= (13, 30)
        size_threshold_scale = 1.0
        if trades_filled_today == 0 and (t_et.hour, t_et.minute) > (11, 0):
            size_threshold_scale = 0.8

        loss_size_multiplier = 0.7 if loss_count_today >= 3 else 1.0

        if (t_et.hour, t_et.minute) < (9, 45):
            signals_this_bar.clear()
            return

        mo_h = int(getattr(config, "MARKET_OPEN_HOUR", 9))
        mo_m = int(getattr(config, "MARKET_OPEN_MINUTE", 30))
        if (t_et.hour, t_et.minute) < (mo_h, mo_m):
            signals_this_bar.clear()
            return

        spy_trend = _compute_spy_trend_from_barbuilder(bar_builder, ETF_SYMBOL)
        spy_dir = (spy_trend.get("direction") or "NEUTRAL").upper()
        min_trade_score = float(getattr(config, "MIN_TRADE_SCORE", 55.0) or 55.0)
        post_min_rel_vol = float(getattr(config, "POST_CONFIRM_MIN_REL_VOL", 1.2) or 1.2)
        post_max_vwap_dist = float(getattr(config, "POST_CONFIRM_MAX_VWAP_DIST_PCT", 0.35) or 0.35)
        post_require_spy_align = bool(getattr(config, "POST_CONFIRM_REQUIRE_SPY_ALIGNMENT", True))
        atr_pct_min = float(getattr(config, "ATR_PCT_MIN", 0.0) or 0.0)
        atr_pct_max = float(getattr(config, "ATR_PCT_MAX", 999.0) or 999.0)

        quality_passed = []
        allow_fast_track_to_trade = bool(getattr(config, "ALLOW_FAST_TRACK_TO_TRADE", False))
        for sig in signals_this_bar:
            if (str(sig.get("confirmation_type") or "") == "fast_track") and not allow_fast_track_to_trade:
                fast_track_trade_block_count += 1
                _append_blocked_candidate_backtest(t_et, sig, "fast_track_trade_blocked")
                continue
            side = (sig.get("side") or "LONG").upper()
            score_ok = float(sig.get("score") or 0.0) >= min_trade_score
            rel_vol_ok = float(sig.get("rel_vol") or 0.0) >= post_min_rel_vol
            vwap_ok = abs(float(sig.get("dist_vwap_pct") or 0.0)) <= post_max_vwap_dist
            atr_pct_sig = float(sig.get("atr_pct") or 0.0)
            atr_ok = atr_pct_min <= atr_pct_sig <= atr_pct_max
            if post_require_spy_align:
                spy_align_ok = side == spy_dir and spy_dir in {"LONG", "SHORT"}
            else:
                spy_align_ok = True
            if score_ok and rel_vol_ok and vwap_ok and atr_ok and spy_align_ok:
                quality_passed.append(sig)
            else:
                post_confirmation_quality_block_count += 1
                _append_blocked_candidate_backtest(t_et, sig, "post_confirmation_quality_failed")

        ranked = rank_and_cap(quality_passed, config.MAX_SIGNALS_PER_DAY)
        signals_this_bar.clear()

        ranked = sorted(ranked, key=lambda s: s["ticker"] in tickers_cancelled_today)
        if len(ranked) < config.MIN_SIGNALS_TO_TRADE:
            return
        if len(positions_today) >= config.MAX_POSITIONS:
            return

        capital_now = capital + sum(t["pnl_dollars"] for t in trades_out)
        if capital_now <= 0:
            return
        max_loss = float(getattr(config, "MAX_DAILY_LOSS_PCT", 0.05))
        if session_start_capital > 0 and (session_start_capital - capital_now) / session_start_capital >= max_loss:
            return

        regime_state = compute_regime_from_barbuilder(bar_builder, ETF_SYMBOL)
        regime_mult = (
            float(regime_state.get("size_multiplier", 1.0))
            if getattr(config, "REGIME_ENGINE_ENABLED", True)
            else 1.0
        )
        last_regime_snapshot.clear()
        last_regime_snapshot.update(dict(regime_state))

        size_cap = capital_now * config.MAX_CAPITAL_PCT_USED

        for rank_pos, sig in enumerate(ranked, 1):
            if len(positions_today) >= config.MAX_POSITIONS:
                break
            ticker = sig["ticker"]
            if ticker in positions_today:
                continue
            if ticker.upper() in ticker_placed_today:
                same_ticker_reentry_block_count += 1
                continue

            side = (sig.get("side") or "LONG").upper()
            if side == "SHORT" and not getattr(config, "ENABLE_SHORTS", True):
                continue

            size_multiplier = 1.0
            if spy_dir != "NEUTRAL" and side != spy_dir:
                size_multiplier *= 0.6
                spy_soft_penalties += 1

            bias_dir = (sig.get("bias_dir") or "NEUTRAL").upper()
            raw_strength = float(sig.get("bias_strength") or 0.0)

            late_day_strength_threshold = 0.35 * size_threshold_scale
            if spy_dir != "NEUTRAL" and late_day and raw_strength < late_day_strength_threshold:
                continue

            weak_bias_threshold = 0.10 * size_threshold_scale
            if raw_strength < weak_bias_threshold:
                size_multiplier *= 0.6
            size_multiplier *= loss_size_multiplier

            atr_pct_sig = float(sig.get("atr_pct") or 0.0)
            if atr_pct_sig < (1.2 * size_threshold_scale):
                sig["score"] = float(sig.get("score") or 0.0) * 0.8

            score = float(sig.get("score") or 0)
            if score < -15:
                continue

            effective_strength = 1.0 if bias_dir == "NEUTRAL" else max(raw_strength, 0.01)

            bar = sig["bar"]
            signal_close = float(bar.get("close") or 0.0)
            if signal_close <= 0:
                continue
            if fill_model == "stress":
                slippage = random.uniform(ENTRY_SLIPPAGE_MIN, ENTRY_SLIPPAGE_MAX)
                if side == "LONG":
                    entry_price = float(bar["high"]) + slippage + ENTRY_SPREAD
                else:
                    entry_price = float(bar["low"]) - slippage - ENTRY_SPREAD
            else:
                entry_price = round(signal_close * (1.001 if side == "LONG" else 0.999), 2)
            if entry_price <= 0:
                continue

            c_close = float((sig.get("bar") or {}).get("close") or 0)
            if not _is_controlled_entry(ticker, side, c_close, sig.get("bar") or {}):
                controlled_entry_block_count += 1
                continue

            if bool(getattr(config, "ENABLE_ENTRY_STRENGTH_GATE", True)):
                sig_bar = sig.get("bar") or {}
                cur_close = float(sig_bar.get("close") or 0.0)
                bar_open = float(sig_bar.get("open") or 0.0)
                prev_closed = float(sig.get("prev_close") or 0.0)
                if side == "LONG":
                    strength_ok = cur_close > bar_open
                else:
                    strength_ok = cur_close < bar_open
                if not strength_ok:
                    entry_strength_block_count += 1
                    _append_blocked_candidate_backtest(t_et, sig, "entry_strength_failed")
                    continue

            dollar, _ = size_per_trade(len(ranked), size_cap, entry_price)
            dollar *= effective_strength * max(0.2, float(size_multiplier) * regime_mult)
            if dollar <= 0:
                continue
            shares = max(1, int(dollar / entry_price))

            # Exit prices (match live "partial + runner cap")
            if side == "LONG":
                tp1_price = entry_price * (1 + TP1_PCT)
                tp2_price = entry_price * (1 + RUN_CAP_PCT)
                stop_price = entry_price * (1 - config.STOP_PCT)
            else:
                tp1_price = entry_price * (1 - TP1_PCT)
                tp2_price = entry_price * (1 - RUN_CAP_PCT)
                stop_price = entry_price * (1 + config.STOP_PCT)

            total_qty = int(shares)
            frac = min(max(float(TP1_FRACTION), 0.0), 1.0)
            tp1_qty = int(round(total_qty * frac))
            if total_qty >= 2:
                tp1_qty = min(max(tp1_qty, 1), total_qty - 1)
            else:
                tp1_qty = 0
            runner_qty = total_qty - tp1_qty if tp1_qty > 0 else total_qty

            ticker_placed_today.add(ticker.upper())
            positions_today.add(ticker)
            n_filled += 1
            trades_filled_today += 1
            pending_trades[ticker] = {
                "side": side,
                "entry_time": t_et.strftime("%H:%M:%S"),
                "entry_dt": t_et,
                "entry_price": entry_price,
                "score": float(sig.get("score") or 0.0),
                "rank_position": int(rank_pos),
                "rel_vol": float(sig.get("rel_vol") or 0.0),
                "atr_pct": float(sig.get("atr_pct") or 0.0),
                "dist_vwap_pct": float(sig.get("dist_vwap_pct") or 0.0),
                "bias_dir": str(sig.get("bias_dir") or "NEUTRAL").upper(),
                "bias_strength": float(sig.get("bias_strength") or 0.0),
                "confirmation_type": str(sig.get("confirmation_type") or "unknown"),
                "tp1_price": tp1_price,
                "tp2_price": tp2_price,
                "stop_price": stop_price,
                "current_stop_price": stop_price,
                "stop_at_breakeven": False,
                "tp1_remaining": float(tp1_qty),
                "runner_remaining": float(runner_qty),
                "high_since_entry": entry_price,
                "low_since_entry": entry_price,
            }

    for t_et, sym, o, h, l, c, vol in events:
        if sym != ETF_SYMBOL and sym not in daily_metrics:
            continue
        last_close[sym] = float(c or 0.0)

        # EOD close (match live runner closing remaining positions)
        if not eod_done and (t_et.hour, t_et.minute) >= (close_hour, close_minute):
            for tk in list(pending_trades.keys()):
                info = pending_trades.get(tk, {})
                side = (info.get("side") or "LONG").upper()
                px = float(last_close.get(tk, 0.0) or 0.0)
                if px <= 0:
                    continue
                tp1_rem = float(info.get("tp1_remaining") or 0.0)
                run_rem = float(info.get("runner_remaining") or 0.0)
                rem = tp1_rem + run_rem
                if rem <= 0:
                    continue
                _emit_trade_row(
                    tk,
                    info,
                    rem,
                    px,
                    "EOD",
                    t_et,
                    float(info.get("tp2_price") or 0.0),
                    float(info.get("current_stop_price") or info.get("stop_price") or 0.0),
                )
                positions_today.discard(tk)
                del pending_trades[tk]
            eod_done = True

        _update_bias(t_et)
        _check_exits(sym, h, l, c, t_et)
        bar_builder.push_ohlcv(sym, o, h, l, c, vol, t_et)
        minute = t_et.minute
        hour = t_et.hour
        boundary = (hour, minute)
        bias_dir = (bias_state.get("direction") or "NEUTRAL").upper()
        sides_to_generate = _sides_from_bias(bias_dir)
        raw_bias_strength = float(bias_state.get("strength") or 0)
        bias_ok_trading = _is_bias_tradeable(bias_state)

        if bias_ok_trading and sym in daily_metrics and sym not in positions_today:
            current = bar_builder.get_current_bar(sym)
            if current and current.get("start_et"):
                try:
                    age_sec = (t_et.timestamp() - current["start_et"].timestamp())
                except Exception:
                    age_sec = 0
                if age_sec >= INTRABAR_MIN_AGE_SECONDS:
                    sm = current["start_et"].minute
                    sh = current["start_et"].hour
                    next_close_m = ((sm // BAR_MINUTES) + 1) * BAR_MINUTES
                    bar_close_h = sh + (next_close_m // 60)
                    bar_close_m = next_close_m % 60
                    bar_key = (bar_close_h, bar_close_m)
                    if last_intrabar_signal_key.get(sym) != bar_key:
                        dm = dict(daily_metrics.get(sym, {}))
                        dm["today_volume_so_far"] = sum(bb["volume"] for bb in bar_builder.get_all_closed(sym))
                        dm["minutes_since_market_open"] = max(1, _minutes_since_market_open(t_et))
                        bar_dict = {
                            "open": current.get("open"), "high": current.get("high"),
                            "low": current.get("low"), "close": current.get("close"),
                            "volume": current.get("volume", 0),
                        }
                        adx = _compute_adx_for_bars(
                            bar_builder.get_all_closed(sym) + [bar_dict],
                            period=int(getattr(config, "ADX_PERIOD", 14)),
                        )
                        bar_dict["adx"] = adx
                        for side_s in sides_to_generate:
                            eligible, score = check_v26_bar_side(sym, bar_dict, dm, side_s, condition_counts)
                            if eligible:
                                if _is_near_support_or_resistance(sym, float(bar_dict.get("close") or 0.0)):
                                    continue
                                score_weighted = score * _bias_weight(side_s, bias_state)
                                prev = float(dm.get("prev_close") or c)
                                avg_vol = float(dm.get("avg_vol_20") or 1)
                                today_vol = dm["today_volume_so_far"] + current.get("volume", 0)
                                pct_1d = (c - prev) / prev * 100 if prev else 0
                                minutes_since_open = max(1, _minutes_since_market_open(t_et))
                                expected_volume = avg_vol * (minutes_since_open / 390.0)
                                rel_vol = (today_vol / max(expected_volume, 1.0)) if avg_vol else 0.0
                                atr_pct = float(dm.get("atr_pct") or 0)
                                vwap = (bar_dict["high"] + bar_dict["low"] + c) / 3.0
                                dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                                close_f = float(c)
                                print(
                                    f"[CHECK controlled entry] {sym} {side_s} close={close_f:.4f} vwap={vwap:.4f}",
                                    flush=True,
                                )
                                if not _is_controlled_entry(sym, side_s, close_f, bar_dict):
                                    controlled_entry_block_count += 1
                                    print(f"[CONTROLLED ENTRY RESULT] FAIL {sym} {side_s}", flush=True)
                                    print(f"Blocked: {sym} {side_s} controlled entry failed", flush=True)
                                    continue
                                print(f"[CONTROLLED ENTRY RESULT] PASS {sym} {side_s}", flush=True)
                                _queue_for_confirmation({
                                    "ticker": sym, "side": side_s, "bias_dir": bias_dir,
                                    "bias_strength": raw_bias_strength,
                                    "score": score_weighted, "bar": bar_dict,
                                    "pct_change_1d": pct_1d, "rel_vol": rel_vol,
                                    "atr_pct": atr_pct, "adx": adx, "dist_vwap_pct": dist_vwap,
                                    "prev_close": prev,
                                }, bar_key)
                        last_intrabar_signal_key[sym] = bar_key

        if minute % BAR_MINUTES == 0 and last_closed_bar_key.get(sym) != boundary:
            last_closed_bar_key[sym] = boundary
            closed = bar_builder.lock_bar(sym, t_et)
            if closed and sym != ETF_SYMBOL:
                _process_confirmations_on_bar_close(sym, closed, boundary)
            if closed and sym in daily_metrics and sym not in positions_today and bias_ok_trading:
                dm = dict(daily_metrics.get(sym, {}))
                dm["today_volume_so_far"] = sum(bb["volume"] for bb in bar_builder.get_all_closed(sym)[:-1])
                dm["minutes_since_market_open"] = max(1, _minutes_since_market_open(t_et))
                adx = _compute_adx_for_bars(
                    bar_builder.get_all_closed(sym),
                    period=int(getattr(config, "ADX_PERIOD", 14)),
                )
                closed_with_adx = dict(closed)
                closed_with_adx["adx"] = adx
                for side_s in sides_to_generate:
                    eligible, score = check_v26_bar_side(sym, closed_with_adx, dm, side_s, condition_counts)
                    if eligible:
                        if _is_near_support_or_resistance(sym, float(closed.get("close") or 0.0)):
                            continue
                        score_weighted = score * _bias_weight(side_s, bias_state)
                        close = float(closed.get("close") or 0)
                        prev = float(dm.get("prev_close") or close)
                        avg_vol = float(dm.get("avg_vol_20") or 1)
                        today_vol = dm["today_volume_so_far"] + closed.get("volume", 0)
                        pct_1d = (close - prev) / prev * 100 if prev else 0
                        minutes_since_open = max(1, _minutes_since_market_open(t_et))
                        expected_volume = avg_vol * (minutes_since_open / 390.0)
                        rel_vol = (today_vol / max(expected_volume, 1.0)) if avg_vol else 0.0
                        atr_pct = float(dm.get("atr_pct") or 0)
                        hc, lc, cc = closed.get("high"), closed.get("low"), close
                        vwap = (hc + lc + cc) / 3.0 if (hc or lc or cc) else 0
                        dist_vwap = (cc - vwap) / vwap * 100 if vwap else 0
                        print(
                            f"[CHECK controlled entry] {sym} {side_s} close={close:.4f} vwap={vwap:.4f}",
                            flush=True,
                        )
                        if not _is_controlled_entry(sym, side_s, close, closed_with_adx):
                            controlled_entry_block_count += 1
                            print(f"[CONTROLLED ENTRY RESULT] FAIL {sym} {side_s}", flush=True)
                            print(f"Blocked: {sym} {side_s} controlled entry failed", flush=True)
                            continue
                        print(f"[CONTROLLED ENTRY RESULT] PASS {sym} {side_s}", flush=True)
                        if side_s == "LONG":
                            signal_hits_long += 1
                        else:
                            signal_hits_short += 1
                        _queue_for_confirmation({
                            "ticker": sym, "side": side_s, "bias_dir": bias_dir,
                            "bias_strength": raw_bias_strength,
                            "score": score_weighted, "bar": closed_with_adx,
                            "pct_change_1d": pct_1d, "rel_vol": rel_vol,
                            "atr_pct": atr_pct, "adx": adx, "dist_vwap_pct": dist_vwap,
                            "prev_close": prev,
                        }, boundary)

        if minute % BAR_MINUTES == 0 and t_et.second >= 5 and signals_this_bar:
            _process_signals(t_et)

    total_pnl = sum(t["pnl_dollars"] for t in trades_out)
    end_capital = start_capital + total_pnl
    diagnostics = {
        "bias_counts": bias_counts,
        "signal_hits_long": signal_hits_long,
        "signal_hits_short": signal_hits_short,
        "condition_counts": condition_counts,
        "confirmation_counts": confirmation_counts,
        "spy_soft_penalties": spy_soft_penalties,
        "controlled_entry_blocks": controlled_entry_block_count,
        "same_ticker_reentry_blocks": same_ticker_reentry_block_count,
        "post_confirmation_quality_blocks": post_confirmation_quality_block_count,
        "entry_strength_blocks": entry_strength_block_count,
        "fast_track_trade_blocks": fast_track_trade_block_count,
        "late_entry_blocks": late_entry_block_count,
        "fast_track_setups_stored": fast_track_setups_stored_count,
        "fast_track_setups_confirmed": fast_track_setups_confirmed_count,
        "fast_track_setups_expired": fast_track_setups_expired_count,
        "fill_model": fill_model,
        "events_processed": len(events),
        "regime_last": dict(last_regime_snapshot),
    }
    return trades_out, end_capital, total_pnl, diagnostics


def _parse_args():
    parser = argparse.ArgumentParser(description="FPLS backtest with configurable date range")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="Start date")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="End date")
    parser.add_argument(
        "--start-capital",
        dest="start_capital",
        type=float,
        default=None,
        help="Starting capital for sizing/reporting. If omitted, uses latest live equity if available.",
    )
    args = parser.parse_args()
    yesterday = _yesterday_et().date()
    date_from = None
    date_to = None
    if args.date_from and str(args.date_from).strip():
        try:
            date_from = datetime.strptime(str(args.date_from).strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    if args.date_to and str(args.date_to).strip():
        try:
            date_to = datetime.strptime(str(args.date_to).strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    # Interactive default: allow manual entry; Enter uses local-yesterday.
    if date_from is None and date_to is None:
        print(f"\nNo dates provided. Enter backtest range (or press Enter for yesterday: {yesterday}):")
        try:
            inp_from = input("  From (YYYY-MM-DD): ").strip()
            inp_to = input("  To   (YYYY-MM-DD): ").strip()
        except EOFError:
            inp_from, inp_to = "", ""
        if not inp_from and not inp_to:
            return yesterday, yesterday, args.start_capital
        if inp_from:
            try:
                date_from = datetime.strptime(inp_from, "%Y-%m-%d").date()
            except ValueError:
                print("  Invalid from date, using yesterday.")
                date_from = yesterday
        if inp_to:
            try:
                date_to = datetime.strptime(inp_to, "%Y-%m-%d").date()
            except ValueError:
                print("  Invalid to date, using from date.")
                date_to = date_from or yesterday
        if date_from is None:
            date_from = date_to
        if date_to is None:
            date_to = date_from
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        return date_from, date_to, args.start_capital
    if date_from is None:
        date_from = date_to
    if date_to is None:
        date_to = date_from
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to, args.start_capital


def _load_latest_live_equity(default_capital: float = 100_000.0) -> float:
    """
    Best-effort latest equity from logs/daily_equity.csv.
    Falls back to default_capital when unavailable/invalid.
    """
    path = os.path.join(config.LOG_DIR, "daily_equity.csv")
    if not os.path.isfile(path):
        return float(default_capital)
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return float(default_capital)
        last = rows[-1]
        eq = float(last.get("Equity", 0) or 0)
        return float(eq) if eq > 0 else float(default_capital)
    except Exception:
        return float(default_capital)


def main():
    date_from, date_to, cli_start_capital = _parse_args()
    days = _trading_days_between(date_from, date_to)
    if not days:
        raise SystemExit("No trading days in range.")
    _p(f"Backtest range: {date_from} to {date_to} ({len(days)} trading days)")

    use_fixed = getattr(config, "USE_FIXED_UNIVERSE", True)
    top_n = int(getattr(config, "WATCHLIST_TOP_N", 150))
    if use_fixed:
        tickers = load_fixed_universe_100()
        _p(f"Universe: first 100 S&P tickers (fixed): {len(tickers)} tickers")
    else:
        tickers = load_all_sp500_tickers()
        _p(f"Universe: per-day rescan from {len(tickers)} S&P candidates, top {top_n} by score")
    t0 = time_mod.time()
    _p("Connecting to IB...")
    ib = connect_ib()
    _p(f"Connected in {time_mod.time() - t0:.1f}s")
    base_start_capital = (
        float(cli_start_capital)
        if cli_start_capital is not None and float(cli_start_capital) > 0
        else _load_latest_live_equity(default_capital=100_000.0)
    )
    if base_start_capital <= 0:
        base_start_capital = 100_000.0
    start_capital = float(base_start_capital)
    all_trades = []
    agg_bias = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
    agg_signal_long = 0
    agg_signal_short = 0
    agg_condition = {"liquidity": 0, "price": 0, "momentum": 0, "volatility": 0, "structural": 0}
    agg_events = 0
    replay_days = 0
    last_diag = {}
    agg_confirmation = {"strict": 0, "fast_track": 0}
    agg_spy_soft_penalties = 0
    agg_controlled_entry_blocks = 0
    agg_same_ticker_reentry_blocks = 0
    agg_post_confirmation_quality_blocks = 0
    agg_entry_strength_blocks = 0
    agg_fast_track_trade_blocks = 0
    agg_late_entry_blocks = 0
    agg_fast_track_setups_stored = 0
    agg_fast_track_setups_confirmed = 0
    agg_fast_track_setups_expired = 0
    fill_model_seen = ""

    try:
        for day_idx, d in enumerate(days):
            date_str = d.strftime("%Y-%m-%d")
            end_dt = EASTERN.localize(datetime.combine(d, datetime.min.time().replace(hour=16, minute=0, second=0, microsecond=0)))
            end_dt_str = _end_dt_str(end_dt)
            _p(f"[{day_idx + 1}/{len(days)}] {date_str}")

            metrics_end_dt = _prior_trading_day_close_et(end_dt)
            prior_date = metrics_end_dt.date()
            metrics_end_dt_str = _end_dt_str(metrics_end_dt)

            if use_fixed:
                # Fixed universe: fetch metrics in parallel (no filters)
                _p("  Fetching metrics (parallel)...")
                t0 = time_mod.time()
                daily_metrics = fetch_daily_metrics_parallel_for_date(
                    ib, tickers, metrics_end_dt_str, max_tickers=len(tickers),
                    use_backtest_volume_min=False, apply_filters=False,
                    progress_callback=lambda c, t: _progress_bar("Metrics", c, t),
                )
                _p(f"  Metrics: {len(daily_metrics)} tickers in {time_mod.time() - t0:.1f}s")
                tickers_day = [s for s in tickers if s in daily_metrics]
            else:
                # Per-day rescan: build watchlist from prior day's data only
                _p("  Rescanning universe for prior day...")
                t0 = time_mod.time()
                tickers_day, daily_metrics = rescan_universe_for_day(
                    prior_date,
                    ib,
                    tickers,
                    top_n,
                    progress_callback=lambda c, t: _progress_bar("Rescan", c, t),
                )
                _p(f"  Rescan: {len(tickers_day)} tickers in {time_mod.time() - t0:.1f}s (top 5: {', '.join(tickers_day[:5])})")

            # Match live Arbiter: at most MAX_REALTIME_SUBSCRIPTIONS streams; ETF ensured like Launch.
            max_subs = int(getattr(config, "MAX_REALTIME_SUBSCRIPTIONS", 100))
            subs = list(tickers_day[:max_subs])
            if ETF_SYMBOL not in subs:
                if len(subs) >= max_subs:
                    subs = subs[:-1] + [ETF_SYMBOL]
                else:
                    subs.append(ETF_SYMBOL)
            tickers_day = [t for t in subs if t != ETF_SYMBOL]
            _p(f"  Subscription universe (live parity): {len(tickers_day)} names + {ETF_SYMBOL}")

            if len(tickers_day) < 10:
                _p(f"  Skipping {date_str}: too few tickers ({len(tickers_day)})")
                continue

            # Fetch 5-sec bars in parallel (SPY + tickers); uses cache when enabled
            all_symbols = subs
            n_to_fetch = sum(1 for s in all_symbols if _load_cached_bars(date_str, s) is None)
            cache_status = "all cached" if n_to_fetch == 0 else f"{n_to_fetch} to fetch"
            _p(f"  Fetching bars (cache={'on' if getattr(config, 'BACKTEST_USE_CACHE', True) else 'off'}, {cache_status})...")
            t0 = time_mod.time()
            all_bars = fetch_5sec_bars_parallel(ib, all_symbols, end_dt_str, date_str)
            _p(f"  Bars fetched in {time_mod.time() - t0:.1f}s")
            etf_bars = all_bars.get(ETF_SYMBOL, [])
            if getattr(config, "BACKTEST_IB_5SEC_ONLY", False):
                if not etf_bars or not bars_look_like_ib_5sec_TRADES(etf_bars):
                    _p(
                        f"  Skipping {date_str}: SPY has no valid IB 5-sec TRADES series "
                        f"(required for bias replay when BACKTEST_IB_5SEC_ONLY)."
                    )
                    continue
            ticker_bars = {s: all_bars.get(s, []) for s in tickers_day}
            if getattr(config, "BACKTEST_IB_5SEC_ONLY", False):
                ticker_bars = {
                    s: b for s, b in ticker_bars.items() if b and bars_look_like_ib_5sec_TRADES(b)
                }
            _p(f"  SPY: {len(etf_bars)} bars, tickers: {sum(1 for v in ticker_bars.values() if v)}/{len(tickers_day)}")
            tickers_day = [s for s in tickers_day if ticker_bars.get(s)]
            if len(tickers_day) < 10:
                _p(f"  Skipping {date_str}: too few bars")
                continue
            dm_day = {s: daily_metrics[s] for s in tickers_day if s in daily_metrics}

            events = build_event_stream(etf_bars, {s: ticker_bars[s] for s in tickers_day})
            if len(events) < 1000:
                _p(f"  Skipping {date_str}: too few events ({len(events)})")
                continue

            _p("  Replaying...")
            trades, end_capital, total_pnl, diag = run_backtest(events, dm_day, start_capital, date_str)
            replay_days += 1
            last_diag = dict(diag or {})
            _p(f"  Done: {len(trades)} trades, PnL ${total_pnl:,.2f}")
            all_trades.extend(trades)
            start_capital = end_capital
            for k in agg_bias:
                agg_bias[k] += diag.get("bias_counts", {}).get(k, 0)
            agg_signal_long += diag.get("signal_hits_long", 0)
            agg_signal_short += diag.get("signal_hits_short", 0)
            for k in agg_condition:
                agg_condition[k] += diag.get("condition_counts", {}).get(k, 0)
            agg_events += diag.get("events_processed", 0)
            conf = diag.get("confirmation_counts", {}) or {}
            agg_confirmation["strict"] += int(conf.get("strict", 0) or 0)
            agg_confirmation["fast_track"] += int(conf.get("fast_track", 0) or 0)
            agg_spy_soft_penalties += int(diag.get("spy_soft_penalties", 0) or 0)
            agg_controlled_entry_blocks += int(diag.get("controlled_entry_blocks", 0) or 0)
            agg_same_ticker_reentry_blocks += int(diag.get("same_ticker_reentry_blocks", 0) or 0)
            agg_post_confirmation_quality_blocks += int(diag.get("post_confirmation_quality_blocks", 0) or 0)
            agg_entry_strength_blocks += int(diag.get("entry_strength_blocks", 0) or 0)
            agg_fast_track_trade_blocks += int(diag.get("fast_track_trade_blocks", 0) or 0)
            agg_late_entry_blocks += int(diag.get("late_entry_blocks", 0) or 0)
            agg_fast_track_setups_stored += int(diag.get("fast_track_setups_stored", 0) or 0)
            agg_fast_track_setups_confirmed += int(diag.get("fast_track_setups_confirmed", 0) or 0)
            agg_fast_track_setups_expired += int(diag.get("fast_track_setups_expired", 0) or 0)
            fill_model_seen = str(diag.get("fill_model", fill_model_seen) or fill_model_seen)

        disconnect_ib(ib)
    except Exception as e:
        disconnect_ib(ib)
        raise SystemExit(f"Fetch/backtest failed: {e}")

    print("\n--- Diagnostics ---", flush=True)
    print("Bias distribution:", agg_bias, flush=True)
    print("Long  signal hits:", agg_signal_long, flush=True)
    print("Short signal hits:", agg_signal_short, flush=True)
    print("Events processed:", agg_events, flush=True)
    print("Per-condition hits:", agg_condition, flush=True)
    print("Controlled-entry blocks:", agg_controlled_entry_blocks, flush=True)
    print("Same-ticker reentry blocks:", agg_same_ticker_reentry_blocks, flush=True)
    print("Post-confirmation quality blocks:", agg_post_confirmation_quality_blocks, flush=True)
    print("Entry-strength blocks:", agg_entry_strength_blocks, flush=True)
    print("Fast-track trade blocks:", agg_fast_track_trade_blocks, flush=True)
    print("Late-entry blocks:", agg_late_entry_blocks, flush=True)
    print("Fast-track setups stored:", agg_fast_track_setups_stored, flush=True)
    print("Fast-track setups confirmed:", agg_fast_track_setups_confirmed, flush=True)
    print("Fast-track setups expired:", agg_fast_track_setups_expired, flush=True)
    if last_diag:
        # Last-day snapshot of parity diagnostics from run_backtest.
        print("Confirmation hits:", last_diag.get("confirmation_counts", {}), flush=True)
        print("SPY soft penalties:", last_diag.get("spy_soft_penalties", 0), flush=True)
        print("Fill model:", last_diag.get("fill_model", "live_parity"), flush=True)

    os.makedirs(config.LOG_DIR, exist_ok=True)
    out_name = f"backtest_trades_{date_from}_{date_to}.csv" if len(days) > 1 else f"backtest_trades_{date_from}.csv"
    path = os.path.join(config.LOG_DIR, out_name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "date", "ticker", "side", "entry_time", "entry_price", "shares", "target", "stop",
            "exit_time", "exit_price", "exit_reason", "pnl_dollars", "pnl_pct",
            "score", "rank_position", "rel_vol", "atr_pct", "dist_vwap_pct",
            "bias_dir", "bias_strength", "confirmation_type", "MFE_pct", "MAE_pct",
            "time_in_trade_minutes",
        ])
        w.writeheader()
        for t in all_trades:
            w.writerow({k: t.get(k, "") for k in w.fieldnames})
    # Keep trade_outcomes.csv in sync for backtest exits/reason analysis.
    init_trade_outcomes_log()
    for t in all_trades:
        log_trade_outcome(
            str(t.get("date") or ""),
            str(t.get("ticker") or ""),
            str(t.get("side") or ""),
            str(t.get("entry_time") or ""),
            str(t.get("exit_time") or ""),
            float(t.get("entry_price") or 0.0),
            float(t.get("exit_price") or 0.0),
            float(t.get("entry_price") or 0.0),
            float(t.get("shares") or 0.0),
            float(t.get("target") or 0.0),
            float(t.get("stop") or 0.0),
            float(t.get("pnl_dollars") or 0.0),
            float(t.get("pnl_pct") or 0.0),
            str(t.get("exit_reason") or ""),
        )
    total_pnl = sum(t["pnl_dollars"] for t in all_trades)
    print(f"\nTrades: {len(all_trades)}", flush=True)
    print(f"Start capital: ${base_start_capital:,.2f}", flush=True)
    print(f"End capital:   ${start_capital:,.2f}", flush=True)
    print(f"Total PnL:     ${total_pnl:,.2f}", flush=True)
    if all_trades:
        wins = sum(1 for t in all_trades if t["pnl_dollars"] > 0)
        print(f"Wins: {wins}/{len(all_trades)}", flush=True)
    print(f"Wrote {path}", flush=True)

    report_path = generate_backtest_report(
        all_trades,
        str(date_from),
        str(date_to),
        start_capital=base_start_capital,
        end_capital=start_capital,  # updated each day in loop
        agg_bias=agg_bias,
        agg_condition=agg_condition,
        signal_hits_long=agg_signal_long,
        signal_hits_short=agg_signal_short,
        events_processed=agg_events,
        trading_days_requested=len(days),
        trading_days_replayed=replay_days,
        diag_summary={
            "confirmation_counts": agg_confirmation,
            "spy_soft_penalties": agg_spy_soft_penalties,
            "controlled_entry_blocks": agg_controlled_entry_blocks,
            "same_ticker_reentry_blocks": agg_same_ticker_reentry_blocks,
            "post_confirmation_quality_blocks": agg_post_confirmation_quality_blocks,
            "entry_strength_blocks": agg_entry_strength_blocks,
            "fast_track_trade_blocks": agg_fast_track_trade_blocks,
            "late_entry_blocks": agg_late_entry_blocks,
            "fast_track_setups_stored": agg_fast_track_setups_stored,
            "fast_track_setups_confirmed": agg_fast_track_setups_confirmed,
            "fast_track_setups_expired": agg_fast_track_setups_expired,
            "fill_model": fill_model_seen or (last_diag.get("fill_model", "live_parity") if last_diag else "live_parity"),
        },
        backtest_trades_csv_path=path,
    )
    if report_path:
        print(f"Backtest report: {report_path}", flush=True)
        rng = f"{date_from}_to_{date_to}" if date_from != date_to else date_from
        gpt_pack = os.path.join(config.REPORT_DIR, f"gpt_backtest_analysis_pack_{rng}.txt")
        if os.path.isfile(gpt_pack):
            print(f"GPT backtest pack: {gpt_pack}", flush=True)


if __name__ == "__main__":
    main()
