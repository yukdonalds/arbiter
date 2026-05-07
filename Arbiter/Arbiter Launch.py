  # -----------------------------
# Fast Paper Long/Short – Runner
# -----------------------------
"""
Run: python "Run Me FPLS.py"

Directional v2.6:
- Determine market bias from ETF (config.ETF_SYMBOL) using EMA(20/50) + slope filter
- Route signals to LONG or SHORT engine (mirrored)
- Execute long and short brackets (TP/SL) with correct PnL
"""

import csv
import os
import time
import logging
import traceback
from datetime import datetime, time as dtime

import pytz

import config
from ib_connection import (
    connect_ib,
    get_account_value,
    get_position_signed,
    get_all_positions,
    disconnect_ib,
    make_stock,
    resolve_account_id,
)
from bar_builder import BarBuilder
from market_bias import get_market_bias_from_closes
from signal_engine import check_v26_bar_side, rank_and_cap
from position_sizing import size_per_trade
from order_execution import (
    actual_fill_price_from_ib,
    place_bracket_exits_side,
    place_market_close,
    place_partial_runner_exits_side,
    place_marketable_limit_entry_side,
    place_stop_order_side,
    runner_secure_stop_price,
)
from trade_logger import init_trade_log, init_equity_log, log_trade, log_daily_equity
from stats_collector import (
    init_signal_log,
    init_trade_outcomes_log,
    init_daily_regime_log,
    log_signal,
    log_trade_outcome,
    log_daily_regime,
)
from daily_report import generate_daily_report
from metrics_cache import (
    is_cache_fresh,
    load_cached_metrics,
    load_cached_watchlist,
    save_cached_metrics,
    save_cached_watchlist,
)
from parallel_fetch import build_watchlist_parallel
from external_screen import build_watchlist_external
from universe_rescan import prior_trading_session_date, rescan_universe_for_day
from regime_engine import compute_regime_from_barbuilder

EASTERN = pytz.timezone("America/New_York")


def load_tickers() -> list[str]:
    out: list[str] = []
    with open(config.SP500_TICKERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.upper())
    return out


def is_kill_switch() -> bool:
    return os.path.isfile(config.KILL_SWITCH_FILE)


def now_et() -> datetime:
    return datetime.now(EASTERN)


def _minutes_since_market_open(ts_et: datetime) -> int:
    """Minutes since regular-session open (default 09:30 ET)."""
    open_h = int(getattr(config, "RTH_OPEN_HOUR", 9))
    open_m = int(getattr(config, "RTH_OPEN_MINUTE", 30))
    market_open = ts_et.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    return int(max(0, (ts_et - market_open).total_seconds() // 60))


def is_after(hour: int, minute: int) -> bool:
    t = now_et().time()
    return (t.hour, t.minute) >= (hour, minute)


def _compute_bias_from_barbuilder(bar_builder: BarBuilder) -> dict:
    sym = getattr(config, "ETF_SYMBOL", "SPY").upper()
    closed = bar_builder.get_all_closed(sym)
    closes = [float(b.get("close") or 0) for b in closed if (b.get("close") or 0) > 0]
    cur = bar_builder.get_current_bar(sym)
    if cur and (cur.get("close") or 0) > 0:
        closes.append(float(cur["close"]))
    return get_market_bias_from_closes(closes)


def _compute_spy_trend_from_barbuilder(bar_builder: BarBuilder) -> dict:
    """
    SPY trend gate for entries:
    Uses BOTH a reduced magnitude threshold AND EMA slope:
    - LONG when abs(diff_pct) > 0.05 AND EMA slope > slope_threshold
    - SHORT when abs(diff_pct) > 0.05 AND EMA slope < -slope_threshold
    - NEUTRAL otherwise
    """
    sym = getattr(config, "ETF_SYMBOL", "SPY").upper()
    closed = bar_builder.get_all_closed(sym)
    closes = [float(b.get("close") or 0) for b in closed if (b.get("close") or 0) > 0]
    cur = bar_builder.get_current_bar(sym)
    if cur and (cur.get("close") or 0) > 0:
        closes.append(float(cur["close"]))
    period = 20
    slope_threshold = 0.01  # normalized small constant; slope==0 will fail strict comparisons

    if len(closes) < period + 1:
        return {
            "direction": "NEUTRAL",
            "price": float(closes[-1]) if closes else 0.0,
            "ma20": 0.0,
            "diff_pct": 0.0,
        }

    price = float(closes[-1])
    alpha = 2.0 / (period + 1.0)

    # EMA seed: SMA of first `period` closes.
    ema = float(sum(closes[:period]) / period)
    ema_prev = None
    # Walk forward to the last close, keeping the last two EMA values.
    for p in closes[period:]:
        ema_prev = ema
        ema = alpha * float(p) + (1.0 - alpha) * ema

    if not ema_prev or ema_prev == 0:
        return {"direction": "NEUTRAL", "price": price, "ma20": ema, "diff_pct": 0.0}

    ema_slope = (ema - ema_prev) / ema_prev  # normalized slope
    diff_pct = (price - ema) / ema * 100.0 if ema else 0.0

    if abs(diff_pct) > 0.05 and ema_slope > slope_threshold:
        direction = "LONG"
    elif abs(diff_pct) > 0.05 and ema_slope < -slope_threshold:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    return {"direction": direction, "price": price, "ma20": float(ema), "diff_pct": float(diff_pct)}


def run():
    log_path = getattr(config, "SESSION_LOG_FILE", os.path.join(config.LOG_DIR, "session.log"))
    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(log_fmt)
    file_handler.setLevel(logging.INFO)
    app_log = logging.getLogger("fpls")
    app_log.setLevel(logging.INFO)
    app_log.handlers.clear()
    app_log.addHandler(file_handler)
    app_log.propagate = False

    def log_info(msg: str) -> None:
        app_log.info(msg)
        print(msg)

    log_info("ARBITER- Startup")
    tickers = load_tickers()
    if not tickers:
        log_info("No tickers in sp500_tickers.txt.")
        return
    log_info(f"Loaded {len(tickers)} tickers from list")

    try:
        log_info("IB startup: connecting...")
        ib = connect_ib()
        log_info("IB startup: connected socket; resolving account...")
        account_id = resolve_account_id(ib)
        if not account_id:
            raise RuntimeError(
                "No usable IB account id found. Set IB_ACCOUNT in config.py or verify TWS account access."
            )
        log_info(f"IB startup: using account {account_id}")
        ib.reqMarketDataType(getattr(config, "MARKET_DATA_TYPE", 3))
        ib.sleep(2)
        log_info("IB startup: reading account values...")
        capital = get_account_value(ib, account_id)
    except Exception as e:
        log_info(f"IB startup failed: {e}")
        return
    log_info(f"Connected. Account: {account_id}, NetLiquidation: ${capital:,.2f}")

    init_trade_log()
    init_equity_log()

    # --- Watchlist + metrics (fixed: cache-first; non-fixed: same rescan as backtest) ---
    stream_tickers = None
    daily_metrics: dict = {}
    top_n = int(getattr(config, "WATCHLIST_TOP_N", 150))
    use_fixed = bool(getattr(config, "USE_FIXED_UNIVERSE", True))
    universe_ready = False

    if not use_fixed:
        prior = prior_trading_session_date(now_et())
        log_info(
            f"USE_FIXED_UNIVERSE=False: backtest-aligned universe "
            f"(metrics as of prior session {prior})..."
        )
        try:
            def _on_rescan(c: int, t: int) -> None:
                if t and (c == 1 or c == t or c % max(1, t // 12) == 0):
                    log_info(f"  Rescan (IB fallback) {c}/{t}")

            wl, dm = rescan_universe_for_day(
                prior, ib, tickers, top_n, progress_callback=_on_rescan
            )
        except Exception as e:
            log_info(f"  Rescan failed: {e}")
            wl, dm = [], {}
        if wl and len(wl) >= 10:
            stream_tickers = wl
            daily_metrics = dm
            save_cached_metrics(daily_metrics)
            save_cached_watchlist(stream_tickers)
            universe_ready = True
            log_info(
                f"  Rescan OK: {len(stream_tickers)} tickers "
                f"(top 5: {', '.join(stream_tickers[:5])})"
            )

    if use_fixed or not universe_ready:
        if is_cache_fresh():
            cached = load_cached_metrics()
            watchlist = load_cached_watchlist()
            if cached and watchlist and not stream_tickers:
                stream_tickers = [s for s in watchlist if s in cached][:top_n]
                daily_metrics = {s: dict(cached[s]) for s in stream_tickers if s in cached}
                log_info(f"Using cached watchlist and metrics: {len(stream_tickers)} tickers (instant)")

        if not stream_tickers or len(daily_metrics) < 50:
            if getattr(config, "USE_EXTERNAL_SCREEN", False):
                result = build_watchlist_external(top_n=top_n)
                if result:
                    watchlist, daily_metrics = result
                    stream_tickers = watchlist
                    if daily_metrics:
                        save_cached_metrics(daily_metrics)
                        save_cached_watchlist(stream_tickers)
                        log_info(f"Saved cache for next run. Using {len(stream_tickers)} tickers (external screen).")
            if not stream_tickers or len(daily_metrics) < 50:
                if getattr(config, "USE_EXTERNAL_SCREEN", False):
                    log_info("External screen did not return enough data; falling back to IB parallel scan.")
                watchlist, daily_metrics = build_watchlist_parallel(ib, tickers, top_n=top_n)
                stream_tickers = watchlist
                if daily_metrics:
                    save_cached_metrics(daily_metrics)
                    save_cached_watchlist(stream_tickers)
                    log_info(f"Saved cache for next run. Using {len(stream_tickers)} tickers (IB parallel).")

    if not stream_tickers:
        stream_tickers = tickers[: min(top_n, len(tickers))]
        log_info(f"Fallback: using first {len(stream_tickers)} tickers (no metrics yet)")
    if not daily_metrics:
        daily_metrics = {
            s: {"avg_vol_20": 0, "atr_pct": 0, "prev_close": 0, "today_volume_so_far": 0} for s in stream_tickers
        }

    init_signal_log()
    init_trade_outcomes_log()
    init_daily_regime_log()

    bar_builder = BarBuilder()
    positions_today: set[str] = set()
    # Block same-ticker reentry only after a confirmed entry fill (not on unfilled placement).
    ticker_placed_today: set[str] = set()
    # Diagnostic only: (ticker, side) recorded on confirmed entry fill (for same-ticker-side block stats).
    ticker_side_filled_today: set[tuple[str, str]] = set()
    start_capital = capital
    peak_capital = capital

    n_signals_today = 0
    trades_filled_today = 0
    trades_exited_today = 0
    win_count_today = 0
    loss_count_today = 0
    total_pnl_today = 0.0
    entry_orders_placed = 0
    entry_orders_filled = 0
    entry_orders_unfilled_or_cancelled = 0
    released_unfilled_entry_orders = 0
    same_ticker_reentry_day_blocks = 0
    same_ticker_side_reentry_day_blocks = 0
    fast_track_direct_trades = 0
    fast_track_setups_stored = 0
    fast_track_setups_confirmed = 0
    fast_track_setups_expired = 0
    controlled_entry_blocks = 0
    entry_strength_blocks = 0
    late_entry_blocks = 0

    pending_entry_trades: dict = {}  # ticker -> {"trade": trade, "placed_at": datetime, "side": "LONG"/"SHORT", ...}
    pending_trades: dict = {}  # ticker -> active trade info (incl side)
    tickers_cancelled_today: set[str] = set()
    last_closed_bar_key: dict[str, tuple[int, int]] = {}
    last_intrabar_signal_key: dict[str, tuple[int, int]] = {}
    signals_this_bar: list[dict] = []
    pending_confirmations: dict[str, list[dict]] = {}
    pending_fast_track_setups: dict[str, list[dict]] = {}
    realtime_streams: dict[str, object] = {}

    def _release_pending_entry_unfilled(ticker: str, trade, detail: str) -> None:
        """Clear pending entry without marking ticker traded; log for diagnostics."""
        nonlocal entry_orders_unfilled_or_cancelled, released_unfilled_entry_orders
        info = pending_entry_trades.get(ticker)
        if not info:
            return
        if trade is not None:
            p_ord = getattr(getattr(info.get("trade"), "order", None), "orderId", None)
            t_ord = getattr(getattr(trade, "order", None), "orderId", None)
            if p_ord is not None and t_ord is not None and int(p_ord) != int(t_ord):
                return
        del pending_entry_trades[ticker]
        entry_orders_unfilled_or_cancelled += 1
        released_unfilled_entry_orders += 1
        log_info(
            f"entry_order_unfilled_released ticker={ticker} reason=entry_order_unfilled_released detail={detail}"
        )

    def _closed_bars_for_controlled(sym: str, bar: dict) -> list[dict]:
        """Closed BarBuilder bars for sym, aligned with the signal bar (no lookahead)."""
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
        """
        Hard gate for entry quality:
        - Must respect favorable VWAP extension cap.
        - Then require either near-VWAP OR a minimum pullback/bounce from recent extreme.
        """
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

        # Reject favorable-direction extension beyond cap.
        if side_u == "LONG" and dist_vwap_pct > max_ext:
            log_info(
                f"ENTRY CHECK values: {sym} {side_u} close={c:.4f}, vwap={vwap:.4f}, "
                f"recent_high=n/a, recent_low=n/a, dist_vwap_pct={dist_vwap_pct:.4f} -> FAIL(ext)"
            )
            return False
        if side_u == "SHORT" and dist_vwap_pct < -max_ext:
            log_info(
                f"ENTRY CHECK values: {sym} {side_u} close={c:.4f}, vwap={vwap:.4f}, "
                f"recent_high=n/a, recent_low=n/a, dist_vwap_pct={dist_vwap_pct:.4f} -> FAIL(ext)"
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
            log_info(
                f"ENTRY CHECK values: {sym} {side_u} close={c:.4f}, vwap={vwap:.4f}, "
                f"recent_high={recent_high:.4f}, recent_low=n/a, dist_vwap_pct={dist_vwap_pct:.4f}, "
                f"pullback_pct={pullback_from_high_pct:.4f}, near_vwap={near_vwap} -> {'PASS' if ok else 'FAIL'}"
            )
            return ok
        if side_u == "SHORT":
            lows = [float(b.get("low") or 0.0) for b in window if float(b.get("low") or 0.0) > 0]
            if not lows:
                return near_vwap
            recent_low = min(lows)
            bounce_from_low_pct = ((c - recent_low) / recent_low * 100.0) if recent_low > 0 else 0.0
            ok = near_vwap or (bounce_from_low_pct >= pullback_pct)
            log_info(
                f"ENTRY CHECK values: {sym} {side_u} close={c:.4f}, vwap={vwap:.4f}, "
                f"recent_high=n/a, recent_low={recent_low:.4f}, dist_vwap_pct={dist_vwap_pct:.4f}, "
                f"bounce_pct={bounce_from_low_pct:.4f}, near_vwap={near_vwap} -> {'PASS' if ok else 'FAIL'}"
            )
            return ok
        return False

    def _append_blocked_csv_simple(sig: dict, side: str, block_reason: str) -> None:
        """Same CSV schema as funnel blocked_candidates (pre-sizing defaults)."""
        path = os.path.join(config.LOG_DIR, "blocked_candidates.csv")
        fields = [
            "Date",
            "Time",
            "ticker",
            "side",
            "score",
            "entry_price",
            "capital_now",
            "capital_for_sizing",
            "len_ranked",
            "bias_strength",
            "effective_strength",
            "size_multiplier",
            "regime_mult",
            "dollar_raw",
            "dollar_final",
            "MIN_POSITION_DOLLARS",
            "block_reason",
        ]
        d = now_et()
        date_str = d.strftime("%Y-%m-%d")
        time_str = d.strftime("%H:%M:%S")
        ep = float((sig.get("bar") or {}).get("close") or 0)
        min_pos = float(getattr(config, "MIN_POSITION_DOLLARS", 1500.0) or 1500.0)
        row = {
            "Date": date_str,
            "Time": time_str,
            "ticker": sig.get("ticker", ""),
            "side": side,
            "score": f"{float(sig.get('score') or 0):.6f}",
            "entry_price": f"{ep:.6f}",
            "capital_now": "0.000000",
            "capital_for_sizing": "0.000000",
            "len_ranked": "0",
            "bias_strength": f"{float(sig.get('bias_strength') or 0):.6f}",
            "effective_strength": "0.000000",
            "size_multiplier": "0.000000",
            "regime_mult": "0.000000",
            "dollar_raw": "0.000000",
            "dollar_final": "0.000000",
            "MIN_POSITION_DOLLARS": f"{min_pos:.6f}",
            "block_reason": block_reason,
        }
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            file_exists = os.path.isfile(path)
            with open(path, "a", newline="", encoding="utf-8") as bf:
                wr = csv.DictWriter(bf, fieldnames=fields)
                if not file_exists:
                    wr.writeheader()
                wr.writerow(row)
        except OSError:
            pass

    # Bias state (updated on schedule from ETF bars)
    bias_state = {"direction": "NEUTRAL", "strength": 0.0}
    last_bias_update_ts = 0.0

    etf_symbol = getattr(config, "ETF_SYMBOL", "SPY").upper()
    min_bias_strength = float(getattr(config, "MIN_BIAS_STRENGTH", 0.6))
    bias_refresh_interval = float(getattr(config, "BIAS_REFRESH_INTERVAL", 300))

    def _is_bias_tradeable(b: dict) -> bool:
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
        """Soft bias: prefer bias-aligned signals (1.2) vs counter (0.8); NEUTRAL = 1.0."""
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

    def _update_bias_if_due(force: bool = False) -> None:
        nonlocal last_bias_update_ts, bias_state
        now_ts = time.time()
        if not force and (now_ts - last_bias_update_ts) < bias_refresh_interval:
            return
        bias_state = _compute_bias_from_barbuilder(bar_builder)
        last_bias_update_ts = now_ts
        log_info(f"Bias: {bias_state.get('direction')} ({float(bias_state.get('strength') or 0):.2f}) via {etf_symbol}")

    def _queue_for_confirmation(sig: dict, signal_bar_key: tuple[int, int]) -> None:
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

    def _process_confirmations_on_bar_close(sym: str, closed_bar: dict, closed_bar_key: tuple[int, int]) -> None:
        nonlocal fast_track_setups_stored, fast_track_setups_confirmed, fast_track_setups_expired
        close_px = float(closed_bar.get("close") or 0.0)
        if close_px <= 0:
            return
        confirmation_min_move_pct = float(getattr(config, "CONFIRMATION_MIN_MOVE_PCT", 0.0) or 0.0)
        setup_confirm_bars = int(getattr(config, "FAST_TRACK_SETUP_CONFIRM_BARS", 2) or 2)
        setups = pending_fast_track_setups.get(sym) or []
        if setups:
            keep_setups: list[dict] = []
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
                    fast_track_setups_confirmed += 1
                    sig = dict(st.get("signal") or {})
                    sig["confirmation_type"] = "strict"
                    sig["confirmed_bars"] = st["bars_waited"]
                    signals_this_bar.append(sig)
                    continue
                if st["bars_waited"] >= setup_confirm_bars:
                    fast_track_setups_expired += 1
                    _append_blocked_csv_simple(dict(st.get("signal") or {}), side, "fast_track_setup_expired")
                    continue
                keep_setups.append(st)
            if keep_setups:
                pending_fast_track_setups[sym] = keep_setups
            else:
                pending_fast_track_setups.pop(sym, None)

        queue = pending_confirmations.get(sym)
        if not queue:
            return
        keep: list[dict] = []
        for item in queue:
            # Only use bars after the signal bar for confirmation.
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
                sig = dict(item.get("signal") or {})
                log_info(f"FAST TRACK USED: {sym} {side} fast_track={fast_track}")
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
                    fast_track_setups_stored += 1
                    pending_fast_track_setups.setdefault(sym, []).append(setup_item)
                else:
                    _append_blocked_csv_simple(sig, side, "fast_track_trade_blocked")
                continue
            if confirmed:
                sig = dict(item.get("signal") or {})
                sig["confirmation_type"] = "strict"
                sig["confirmed_bars"] = item["bars_checked"]
                signals_this_bar.append(sig)
                log_info(
                    f"Confirmed: {sym} {side} on bar {item['bars_checked']}/1 "
                    f"(close={close_px:.2f}, level={confirm_level:.2f}, fast_track={fast_track})"
                )
                continue
            # Allow up to two confirmation bars before dropping.
            if item["bars_checked"] >= 2:
                log_info(
                    f"Skipped: {sym} {side} confirmation failed "
                    f"(close={close_px:.2f}, level={confirm_level:.2f})"
                )
                continue
            keep.append(item)
        if keep:
            pending_confirmations[sym] = keep
        else:
            pending_confirmations.pop(sym, None)

    def _mfe_mae_pct(side: str, entry: float, high: float, low: float) -> tuple[float, float]:
        if entry <= 0:
            return 0.0, 0.0
        side = (side or "").upper()
        if side == "SHORT":
            # MFE is drop from entry to low; MAE is rise from entry to high (negative value)
            mfe = (entry - low) / entry * 100.0
            mae = (entry - high) / entry * 100.0
            return float(mfe), float(mae)
        mfe = (high - entry) / entry * 100.0
        mae = (low - entry) / entry * 100.0
        return float(mfe), float(mae)

    def _compute_adx_for_bars(bars: list[dict], period: int = 14) -> float:
        """
        Compute ADX (Wilder) from a list of OHLC bars.
        Requires enough bars; returns 0.0 when insufficient.
        """
        if not bars or len(bars) < (2 * period + 1):
            return 0.0

        # Use the latest subset to limit work.
        sub = bars[-(2 * period + 1) :]
        highs = [float(b.get("high") or 0.0) for b in sub]
        lows = [float(b.get("low") or 0.0) for b in sub]
        closes = [float(b.get("close") or 0.0) for b in sub]

        # True Range and directional movement arrays (length = len(sub)-1)
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

        # Wilder smoothing init over the first `period` elements.
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

        # i is the index into tr/DM arrays.
        # First DX computed corresponds to i=period-1 using the initialized sm_*.
        for i in range(period - 1, len(tr)):
            if i == period - 1:
                # Use init sm_tr/sm_plus/sm_minus
                pass
            else:
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

        # ADX = Wilder smoothing of DX: start with SMA over first `period` DX values.
        adx = sum(dxs[:period]) / period
        for k in range(period, len(dxs)):
            adx = ((adx * (period - 1)) + dxs[k]) / period
        return float(adx)

    def _is_near_support_or_resistance(sym: str, close_price: float) -> bool:
        """
        Reject entries that are too close to obvious support/resistance extremes
        over a short rolling window.
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

    def on_5sec_bar(sym, bars_list, has_new_bar):
        nonlocal signals_this_bar, controlled_entry_blocks
        if not has_new_bar or not bars_list:
            return
        bar = bars_list[-1]

        # Update MFE/MAE + trailing stops for open positions (direction-aware)
        if sym in pending_trades:
            info = pending_trades[sym]
            side = (info.get("side") or "LONG").upper()
            ep = float(info.get("entry_price") or 0.0)
            if ep > 0:
                high = float(getattr(bar, "high", None) or getattr(bar, "close", ep))
                low = float(getattr(bar, "low", None) or getattr(bar, "close", ep))
                info["high_since_entry"] = max(float(info.get("high_since_entry", ep)), high)
                info["low_since_entry"] = min(float(info.get("low_since_entry", ep)), low)

            # Time-based quality exits (parity with backtest):
            #  - EARLY_LOSS_EXIT: >=20m open and unrealized <= -1.0%
            #  - WEAKNESS_EXIT: >=45m open, adverse to entry, MFE < +0.5%
            #  - STALE_EXIT: >=90m open, unrealized in [-0.3%, +0.3%]
            # These exits place a market close and let the fill handler write logs.
            if ep > 0 and not info.get("force_exit_pending"):
                entry_dt = info.get("entry_dt")
                if isinstance(entry_dt, datetime):
                    open_minutes = max(0.0, (now_et() - entry_dt).total_seconds() / 60.0)
                else:
                    open_minutes = 0.0
                high_se = float(info.get("high_since_entry", ep))
                low_se = float(info.get("low_since_entry", ep))
                mfe_pct, _ = _mfe_mae_pct(side, ep, high_se, low_se)
                cur_price = float(getattr(bar, "close", ep) or ep)
                unrealized_pct = (
                    ((cur_price - ep) / ep * 100.0)
                    if side == "LONG"
                    else ((ep - cur_price) / ep * 100.0)
                )
                early_loss_enabled = bool(getattr(config, "ENABLE_EARLY_LOSS_EXIT", True))
                early_loss_min_minutes = float(getattr(config, "EARLY_LOSS_EXIT_MINUTES", 20) or 20)
                early_loss_pct = float(getattr(config, "EARLY_LOSS_EXIT_PCT", -1.0) or -1.0)
                early_loss_require_low_mfe = bool(getattr(config, "EARLY_LOSS_EXIT_REQUIRE_LOW_MFE", False))
                early_loss_mfe_threshold = float(getattr(config, "EARLY_LOSS_EXIT_MFE_THRESHOLD", 0.5) or 0.5)
                early_loss_exit = (
                    early_loss_enabled
                    and open_minutes >= early_loss_min_minutes
                    and unrealized_pct <= early_loss_pct
                    and (not early_loss_require_low_mfe or mfe_pct < early_loss_mfe_threshold)
                )
                weakness_exit = (
                    open_minutes >= 30.0
                    and mfe_pct < 0.5
                    and (
                        (side == "LONG" and ((cur_price - ep) / ep * 100.0) <= -0.3)
                        or (side == "SHORT" and ((ep - cur_price) / ep * 100.0) <= -0.3)
                    )
                )
                stale_exit = open_minutes >= 90.0 and (-0.3 <= unrealized_pct <= 0.3)
                if early_loss_exit:
                    forced_reason = "EARLY_LOSS_EXIT"
                elif weakness_exit:
                    forced_reason = "WEAKNESS_EXIT"
                elif stale_exit:
                    forced_reason = "STALE_EXIT"
                else:
                    forced_reason = ""
                if forced_reason:
                    try:
                        for key in ("stop_trade", "tp1_trade", "tp2_trade"):
                            tr = info.get(key)
                            if tr and getattr(getattr(tr, "orderStatus", None), "status", "") != "Filled":
                                ib.cancelOrder(tr.order)
                    except Exception:
                        pass
                    try:
                        place_market_close(ib, sym, account_id)
                        info["force_exit_pending"] = True
                        info["force_exit_reason"] = forced_reason
                        pending_trades[sym] = info
                        log_info(
                            f"  Forced exit: {sym} {side} reason={forced_reason} "
                            f"open_min={open_minutes:.1f} mfe={mfe_pct:.2f}% unr={unrealized_pct:.2f}%"
                        )
                    except Exception as e:
                        log_info(f"  Forced exit failed {sym} {side} reason={forced_reason}: {e}")
                    return

            trail_be_mfe = float(getattr(config, "TRAIL_BREAKEVEN_MFE_PCT", 2.0))
            trail_act_mfe = float(getattr(config, "TRAIL_ACTIVATE_MFE_PCT", 3.0))
            trail_dist_pct = float(getattr(config, "TRAIL_DISTANCE_PCT", 1.5))
            hard_be_trigger_pct = 0.8
            stop_trade = info.get("stop_trade")
            if stop_trade and ep > 0:
                try:
                    pos = float(get_position_signed(ib, account_id, sym))
                    shares = float(info.get("shares") or 0.0)
                    if shares <= 0:
                        return
                    # Ensure we still have the expected position direction
                    if side == "LONG" and pos < shares:
                        return
                    if side == "SHORT" and pos > -shares:
                        return

                    # --- POST-CUTOFF TRADE PROTECTION (14:00 ET) ---
                    after_cutoff = (now_et().hour, now_et().minute) >= (14, 0)
                    if after_cutoff:
                        cur_price = float(getattr(bar, "close", ep) or ep)
                        unrealized_pnl = (cur_price - ep) * shares if side == "LONG" else (ep - cur_price) * shares
                        if unrealized_pnl > 0:
                            # Tighten trailing distance when we're already green.
                            trail_dist_pct = float(trail_dist_pct) * 0.6
                            secure_stop = runner_secure_stop_price(ep, side)
                            current_stop = float(info.get("current_stop_price") or 0.0)
                            already_breakeven = bool(info.get("stop_at_breakeven"))
                            should_move = (
                                (not already_breakeven)
                                or (side == "LONG" and secure_stop > current_stop)
                                or (side == "SHORT" and (current_stop <= 0 or secure_stop < current_stop))
                            )
                            if should_move:
                                ib.cancelOrder(stop_trade.order)
                                new_sl = place_stop_order_side(ib, sym, shares, secure_stop, side, account_id)
                                info["stop_trade"] = new_sl
                                stop_trade = new_sl
                                info["current_stop_price"] = secure_stop
                                info["stop_at_breakeven"] = True
                                log_info(
                                    f"  Post-cutoff protection: {sym} {side} green pnl; stop->{secure_stop:.2f}, trail_dist_pct={trail_dist_pct:.2f}"
                                )

                    high_se = float(info.get("high_since_entry", ep))
                    low_se = float(info.get("low_since_entry", ep))
                    mfe_pct, _ = _mfe_mae_pct(side, ep, high_se, low_se)
                    cur_price = float(getattr(bar, "close", ep) or ep)
                    unrealized_pct = (
                        ((cur_price - ep) / ep * 100.0)
                        if side == "LONG"
                        else ((ep - cur_price) / ep * 100.0)
                    )

                    if unrealized_pct >= hard_be_trigger_pct:
                        be_stop = ep * (1.001 if side == "LONG" else 0.999)
                        current_stop = float(info.get("current_stop_price") or 0.0)
                        should_move = (
                            (side == "LONG" and be_stop > current_stop)
                            or (side == "SHORT" and (current_stop <= 0 or be_stop < current_stop))
                        )
                        if should_move:
                            ib.cancelOrder(stop_trade.order)
                            new_sl = place_stop_order_side(ib, sym, shares, be_stop, side, account_id)
                            info["stop_trade"] = new_sl
                            stop_trade = new_sl
                            info["current_stop_price"] = be_stop
                            info["stop_at_breakeven"] = True
                            log_info(f"  Break-even lock: {sym} {side} stop moved to {be_stop:.2f} at {unrealized_pct:.2f}%")

                    if mfe_pct >= trail_be_mfe and not info.get("stop_at_breakeven"):
                        ib.cancelOrder(stop_trade.order)
                        secure_stop = runner_secure_stop_price(ep, side)
                        new_sl = place_stop_order_side(ib, sym, shares, secure_stop, side, account_id)
                        info["stop_trade"] = new_sl
                        info["current_stop_price"] = secure_stop
                        info["stop_at_breakeven"] = True
                        log_info(f"  Trail: {sym} {side} stop moved to secure @ {secure_stop:.2f} (entry {ep:.2f})")
                    elif mfe_pct >= trail_act_mfe:
                        current_stop = float(info.get("current_stop_price") or 0.0)
                        if side == "LONG":
                            new_stop = high_se * (1 - trail_dist_pct / 100.0)
                            if new_stop > current_stop and new_stop > ep:
                                ib.cancelOrder(stop_trade.order)
                                new_sl = place_stop_order_side(ib, sym, shares, new_stop, side, account_id)
                                info["stop_trade"] = new_sl
                                info["current_stop_price"] = new_stop
                                log_info(f"  Trail: {sym} {side} stop moved to {new_stop:.2f}")
                        else:
                            new_stop = low_se * (1 + trail_dist_pct / 100.0)
                            # For shorts, stop should move DOWN (toward profit) i.e. smaller number
                            if (current_stop <= 0 or new_stop < current_stop) and new_stop < ep:
                                ib.cancelOrder(stop_trade.order)
                                new_sl = place_stop_order_side(ib, sym, shares, new_stop, side, account_id)
                                info["stop_trade"] = new_sl
                                info["current_stop_price"] = new_stop
                                log_info(f"  Trail: {sym} {side} stop moved to {new_stop:.2f}")
                except Exception as e:
                    log_info(f"  Trail stop update failed {sym}: {e}")

        t = getattr(bar, "time", None) or getattr(bar, "date", None) or now_et()
        if t and hasattr(t, "tzinfo") and t.tzinfo is None:
            t = EASTERN.localize(t)
        if not t:
            t = now_et()
        # Use full OHLCV (matches backtest push_ohlcv; IB RealTimeBar has open_, high, low, close, volume)
        o = float(getattr(bar, "open_", getattr(bar, "open", bar.close)))
        h = float(getattr(bar, "high", bar.close))
        l_ = float(getattr(bar, "low", bar.close))
        c = float(bar.close)
        v = max(0, float(getattr(bar, "volume", 0)))
        bar_builder.push_ohlcv(sym, o, h, l_, c, v, t)

        minute = t.minute
        hour = t.hour
        boundary = (hour, minute)

        # Signal generation uses current bias (updated on schedule in the main loop)
        bias_dir = (bias_state.get("direction") or "NEUTRAL").upper()
        sides_to_generate = _sides_from_bias(bias_dir)
        raw_bias_strength = float(bias_state.get("strength") or 0.0)
        bias_ok_trading = _is_bias_tradeable(bias_state)

        # Intrabar
        intrabar_min_age = getattr(config, "INTRABAR_MIN_AGE_SECONDS", 60)
        current = bar_builder.get_current_bar(sym)
        if (
            bias_ok_trading
            and current
            and current.get("start_et")
            and sym not in positions_today
            and sym not in pending_entry_trades
            and sym in daily_metrics
        ):
            try:
                start_et = current["start_et"]
                age_sec = (t.timestamp() - start_et.timestamp())
            except Exception:
                age_sec = 0
            if age_sec >= intrabar_min_age:
                # Bar-close key for this bar (avoid firing again at close)
                sm = start_et.minute
                sh = start_et.hour
                next_close_m = ((sm // config.BAR_MINUTES) + 1) * config.BAR_MINUTES
                bar_close_h = sh + (next_close_m // 60)
                bar_close_m = next_close_m % 60
                bar_key = (bar_close_h, bar_close_m)
                if last_intrabar_signal_key.get(sym) != bar_key:
                    dm = dict(daily_metrics.get(sym, {}))
                    dm["today_volume_so_far"] = sum(b["volume"] for b in bar_builder.get_all_closed(sym))
                    dm["minutes_since_market_open"] = max(1, _minutes_since_market_open(t))
                    bar_dict = {
                        "open": current.get("open"),
                        "high": current.get("high"),
                        "low": current.get("low"),
                        "close": current.get("close"),
                        "volume": current.get("volume", 0),
                    }
                    # ADX is needed for (a) adaptive confirmation and (b) neutral-market fallback.
                    adx = _compute_adx_for_bars(bar_builder.get_all_closed(sym) + [bar_dict], period=int(getattr(config, "ADX_PERIOD", 14)))
                    bar_dict["adx"] = adx
                    for side_s in sides_to_generate:
                        eligible, score = check_v26_bar_side(sym, bar_dict, dm, side_s)
                        if eligible:
                            if _is_near_support_or_resistance(sym, float(bar_dict.get("close") or 0.0)):
                                log_info(f"Skipped: {sym} {side_s} near support/resistance zone")
                                continue
                            score_weighted = score * _bias_weight(side_s, bias_state)
                            close = float(bar_dict.get("close") or 0)
                            prev = float(dm.get("prev_close") or close)
                            avg_vol = float(dm.get("avg_vol_20") or 1)
                            today_vol = float(dm.get("today_volume_so_far", 0) + float(bar_dict.get("volume") or 0))
                            print(f"{sym} avg_vol_20={avg_vol} today_vol={today_vol}")
                            pct_change_1d = (close - prev) / prev * 100 if prev else 0
                            minutes_since_open = max(1, _minutes_since_market_open(t))
                            expected_volume = avg_vol * (minutes_since_open / 390.0)
                            rel_vol = (today_vol / max(expected_volume, 1.0)) if avg_vol else 0.0
                            atr_pct = float(dm.get("atr_pct") or 0)
                            h, l_, c = float(bar_dict.get("high") or close), float(bar_dict.get("low") or close), close
                            vwap = (h + l_ + c) / 3.0 if (h or l_ or c) else 0
                            dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                            sig_stub = {
                                "ticker": sym,
                                "side": side_s,
                                "bias_strength": raw_bias_strength,
                                "score": score_weighted,
                                "bar": bar_dict,
                            }
                            log_info(
                                f"CHECK controlled entry: {sym} {side_s} close={close:.4f} vwap={vwap:.4f}"
                            )
                            if not _is_controlled_entry(sym, side_s, close, bar_dict):
                                controlled_entry_blocks += 1
                                _append_blocked_csv_simple(sig_stub, side_s, "controlled_entry_filter_failed")
                                log_info(f"CONTROLLED ENTRY RESULT: FAIL")
                                log_info(f"Blocked: {sym} {side_s} controlled entry failed")
                                continue
                            log_info("CONTROLLED ENTRY RESULT: PASS")
                            _queue_for_confirmation(
                                {
                                    "ticker": sym,
                                    "side": side_s,
                                    "bias_dir": bias_dir,
                                    "bias_strength": raw_bias_strength,
                                    "score": score_weighted,
                                    "bar": bar_dict,
                                    "pct_change_1d": pct_change_1d,
                                    "rel_vol": rel_vol,
                                    "atr_pct": atr_pct,
                                    "adx": adx,
                                    "dist_vwap_pct": dist_vwap,
                                    "prev_close": prev,
                                },
                                bar_key,
                            )
                    last_intrabar_signal_key[sym] = bar_key

        # Bar close
        if minute % config.BAR_MINUTES == 0 and last_closed_bar_key.get(sym) != boundary:
            last_closed_bar_key[sym] = boundary
            closed = bar_builder.lock_bar(sym, t)
            if closed and sym != etf_symbol:
                _process_confirmations_on_bar_close(sym, closed, boundary)
            if not closed or sym in positions_today or sym in pending_entry_trades or sym not in daily_metrics:
                return
            if not bias_ok_trading:
                return
            dm = dict(daily_metrics.get(sym, {}))
            dm["today_volume_so_far"] = sum(b["volume"] for b in bar_builder.get_all_closed(sym)[:-1])
            dm["minutes_since_market_open"] = max(1, _minutes_since_market_open(t))
            adx = _compute_adx_for_bars(bar_builder.get_all_closed(sym), period=int(getattr(config, "ADX_PERIOD", 14)))
            closed_with_adx = dict(closed)
            closed_with_adx["adx"] = adx
            for side_s in sides_to_generate:
                eligible, score = check_v26_bar_side(sym, closed_with_adx, dm, side_s)
                if eligible:
                    if _is_near_support_or_resistance(sym, float(closed.get("close") or 0.0)):
                        log_info(f"Skipped: {sym} {side_s} near support/resistance zone")
                        continue
                    score_weighted = score * _bias_weight(side_s, bias_state)
                    close = float(closed.get("close") or 0)
                    prev = float(dm.get("prev_close") or close)
                    avg_vol = float(dm.get("avg_vol_20") or 1)
                    today_vol = float(dm.get("today_volume_so_far", 0) + float(closed.get("volume") or 0))
                    print(f"{sym} avg_vol_20={avg_vol} today_vol={today_vol}")
                    pct_change_1d = (close - prev) / prev * 100 if prev else 0
                    minutes_since_open = max(1, _minutes_since_market_open(t))
                    expected_volume = avg_vol * (minutes_since_open / 390.0)
                    rel_vol = (today_vol / max(expected_volume, 1.0)) if avg_vol else 0.0
                    atr_pct = float(dm.get("atr_pct") or 0)
                    h, l_, c = float(closed.get("high") or close), float(closed.get("low") or close), close
                    vwap = (h + l_ + c) / 3.0 if (h or l_ or c) else 0
                    dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                    sig_stub = {
                        "ticker": sym,
                        "side": side_s,
                        "bias_strength": raw_bias_strength,
                        "score": score_weighted,
                        "bar": closed_with_adx,
                    }
                    log_info(
                        f"CHECK controlled entry: {sym} {side_s} close={close:.4f} vwap={vwap:.4f}"
                    )
                    if not _is_controlled_entry(sym, side_s, close, closed_with_adx):
                        controlled_entry_blocks += 1
                        _append_blocked_csv_simple(sig_stub, side_s, "controlled_entry_filter_failed")
                        log_info("CONTROLLED ENTRY RESULT: FAIL")
                        log_info(f"Blocked: {sym} {side_s} controlled entry failed")
                        continue
                    log_info("CONTROLLED ENTRY RESULT: PASS")
                    _queue_for_confirmation(
                        {
                            "ticker": sym,
                            "side": side_s,
                            "bias_dir": bias_dir,
                            "bias_strength": raw_bias_strength,
                            "score": score_weighted,
                            "bar": closed_with_adx,
                            "pct_change_1d": pct_change_1d,
                            "rel_vol": rel_vol,
                            "atr_pct": atr_pct,
                            "adx": adx,
                            "dist_vwap_pct": dist_vwap,
                            "prev_close": prev,
                        },
                        boundary,
                    )

    def _wrap_rt_bar(symbol: str):
        """So a bug in one ticker's bar path cannot kill the whole session (see ~first RTH bar)."""

        def _handler(bars_list, has_new_bar):
            try:
                on_5sec_bar(symbol, bars_list, has_new_bar)
            except Exception as e:
                log_info(
                    f"on_5sec_bar({symbol}) error (continuing): {e}\n{traceback.format_exc()}"
                )

        return _handler

    def place_bracket_on_entry_fill(ticker: str, filled: float, avg_price: float, entry_info: dict | None = None) -> bool:
        nonlocal trades_filled_today, entry_orders_filled, ticker_side_filled_today
        if filled <= 0 or avg_price <= 0:
            return False
        if ticker in pending_trades:
            return True

        info = pending_entry_trades.pop(ticker, {}) if ticker in pending_entry_trades else {}
        if entry_info is None:
            entry_info = info
        side = (entry_info.get("side") or "LONG").upper()

        pending_trades[ticker] = {"_placing_bracket": True}
        try:
            exits = place_partial_runner_exits_side(
                ib,
                ticker,
                filled,
                float(avg_price),
                side,
                account_id,
                atr_pct=float(entry_info.get("atr_pct") or 0.0),
            )
        except Exception as e:
            log_info(f"  Bracket placement failed {ticker}: {e}")
            del pending_trades[ticker]
            return False

        entry_time = now_et().strftime("%H:%M:%S")
        signal_price = float(entry_info.get("signal_price") or avg_price)
        slippage_pct = ((float(avg_price) - signal_price) / signal_price * 100) if signal_price else 0.0

        log_signal(
            now_et().strftime("%Y-%m-%d"),
            entry_time,
            ticker,
            side,
            entry_info.get("bias_dir", "NEUTRAL"),
            float(entry_info.get("bias_strength") or 0.0),
            entry_info.get("pct_change_1d", 0),
            entry_info.get("rel_vol", 0),
            entry_info.get("atr_pct", 0),
            entry_info.get("dist_vwap_pct", 0),
            entry_info.get("score", 0),
            entry_info.get("rank_position", 0),
            signal_price,
            filled=True,
            fill_price=avg_price,
            fill_time=entry_time,
            slippage_pct=slippage_pct,
            time_to_fill_sec=(now_et() - entry_info.get("placed_at", now_et())).total_seconds()
            if entry_info.get("placed_at")
            else 0,
        )

        pending_trades[ticker] = {
            "side": side,
            "entry_time": entry_time,
            "entry_price": float(avg_price),
            # Runner size is what trailing/stop management operates on.
            "shares": float(exits.get("runner_qty") or filled),
            "full_shares": float(filled),
            "tp1_shares": float(exits.get("tp1_qty") or 0.0),
            "tp1_target": float(exits.get("tp1_price") or 0.0),
            "runner_target": float(exits.get("tp2_price") or 0.0),
            "stop": float(exits.get("stop_price") or 0.0),
            "current_stop_price": float(exits.get("stop_price") or 0.0),
            "stop_trade": exits.get("sl_trade"),
            "tp1_trade": exits.get("tp1_trade"),
            "tp2_trade": exits.get("tp2_trade"),
            "signal_price": signal_price,
            "pct_change_1d": entry_info.get("pct_change_1d", 0),
            "rel_vol": entry_info.get("rel_vol", 0),
            "atr_pct": entry_info.get("atr_pct", 0),
            "dist_vwap_pct": entry_info.get("dist_vwap_pct", 0),
            "score": entry_info.get("score", 0),
            "rank_position": entry_info.get("rank_position", 0),
            "high_since_entry": float(avg_price),
            "low_since_entry": float(avg_price),
            "entry_dt": now_et(),
        }
        positions_today.add(ticker)
        ticker_placed_today.add(ticker.upper())
        ticker_side_filled_today.add((ticker.upper(), side))
        entry_orders_filled += 1
        trades_filled_today += 1
        log_info(
            f"  Filled entry: {ticker} {side} @ {avg_price:.2f} x {filled} -> "
            f"TP1={float(exits.get('tp1_price') or 0):.2f} ({float(exits.get('tp1_qty') or 0):.0f} sh), "
            f"RUN_CAP={float(exits.get('tp2_price') or 0):.2f} ({float(exits.get('runner_qty') or 0):.0f} sh), "
            f"STP={float(exits.get('stop_price') or 0):.2f}"
        )
        return True

    def _handle_exit_fill(ticker: str, exit_price: float, filled_qty: float | None = None, order_id: int | None = None):
        nonlocal trades_exited_today, win_count_today, loss_count_today, total_pnl_today
        if ticker not in pending_trades:
            return
        info = pending_trades[ticker]
        if info.get("_placing_bracket"):
            return

        # Partial TP1 fill: log it, keep managing the runner.
        tp1_trade = info.get("tp1_trade")
        tp1_order_id = getattr(getattr(tp1_trade, "order", None), "orderId", None) if tp1_trade else None
        if order_id is not None and tp1_order_id is not None and int(order_id) == int(tp1_order_id):
            qty = float(filled_qty or 0.0)
            if qty <= 0:
                qty = float(info.get("tp1_shares") or 0.0)
            if qty <= 0:
                return

            side = (info.get("side") or "LONG").upper()
            exit_time = now_et().strftime("%H:%M:%S")
            date_str = now_et().strftime("%Y-%m-%d")
            entry_price = float(info["entry_price"])

            if exit_price <= 0 or exit_price != exit_price:
                log_info(
                    f"  TP1 fill ignored for trade log: {ticker} invalid exit price {exit_price!r} "
                    "(not writing trades.csv row; check fill price from IB)"
                )
                info["tp1_shares"] = max(0.0, float(info.get("tp1_shares") or 0.0) - qty)
                pending_trades[ticker] = info
                return

            if side == "SHORT":
                pnl_d = (entry_price - exit_price) * qty
                pnl_pct = ((entry_price - exit_price) / entry_price * 100.0) if entry_price else 0.0
            else:
                pnl_d = (exit_price - entry_price) * qty
                pnl_pct = ((exit_price / entry_price - 1) * 100.0) if entry_price else 0.0

            ep = entry_price
            sp = float(info.get("signal_price", ep))
            high_se = float(info.get("high_since_entry", ep))
            low_se = float(info.get("low_since_entry", ep))
            mfe_pct, mae_pct = _mfe_mae_pct(side, ep, high_se, low_se)
            hit_3_before_3 = mfe_pct >= 3 and mae_pct > -3
            slippage_entry = ((ep - sp) / sp * 100.0) if sp else 0.0

            log_trade(
                date_str,
                ticker,
                side,
                info["entry_time"],
                entry_price,
                qty,
                float(info.get("tp1_target", 0)),
                float(info.get("stop", 0)),
                exit_time,
                exit_price,
                pnl_d,
                pnl_pct,
            )
            log_trade_outcome(
                date_str,
                ticker,
                side,
                info["entry_time"],
                exit_time,
                entry_price,
                exit_price,
                sp,
                qty,
                float(info.get("tp1_target", 0)),
                float(info.get("stop", 0)),
                pnl_d,
                pnl_pct,
                "TP1",
                mfe_pct=mfe_pct,
                mae_pct=mae_pct,
                hit_3_before_3=hit_3_before_3,
                slippage_entry_pct=slippage_entry,
                slippage_exit_pct=0,
            )

            total_pnl_today += pnl_d
            if pnl_d > 0:
                win_count_today += 1
            else:
                loss_count_today += 1
            score = float(info.get("score") or 0.0)
            log_info(f"TRADE | {ticker} | score={score:.2f} | pnl={pnl_d:.2f}")
            log_info(f"  Filled TP1: {ticker} {side} @ {exit_price:.2f} x {qty:.0f} PnL ${pnl_d:.2f} ({pnl_pct:.2f}%)")
            # Reduce remaining TP1 shares so we don't double count.
            info["tp1_shares"] = max(0.0, float(info.get("tp1_shares") or 0.0) - qty)
            pending_trades[ticker] = info
            return

        side = (info.get("side") or "LONG").upper()
        exit_time = now_et().strftime("%H:%M:%S")
        date_str = now_et().strftime("%Y-%m-%d")
        now_exit = now_et()
        forced_exit_reason = str(info.get("force_exit_reason") or "").strip().upper()

        entry_price = float(info["entry_price"])
        shares = float(filled_qty or info["shares"])
        if side == "SHORT":
            pnl_d = (entry_price - exit_price) * shares
            pnl_pct = ((entry_price - exit_price) / entry_price * 100.0) if entry_price else 0.0
        else:
            pnl_d = (exit_price - entry_price) * shares
            pnl_pct = ((exit_price / entry_price - 1) * 100.0) if entry_price else 0.0
        if forced_exit_reason:
            exit_reason = forced_exit_reason
        elif side == "SHORT":
            cap = float(info.get("runner_target") or info.get("target") or 0.0)
            if (now_exit.hour, now_exit.minute) >= (
                getattr(config, "CLOSE_POSITIONS_HOUR", 15),
                getattr(config, "CLOSE_POSITIONS_MINUTE", 45),
            ):
                exit_reason = "EOD"
            elif cap and exit_price <= cap * 1.001:
                exit_reason = "TP"
            else:
                exit_reason = "STP"
        else:
            cap = float(info.get("runner_target") or info.get("target") or 0.0)
            if (now_exit.hour, now_exit.minute) >= (
                getattr(config, "CLOSE_POSITIONS_HOUR", 15),
                getattr(config, "CLOSE_POSITIONS_MINUTE", 45),
            ):
                exit_reason = "EOD"
            elif cap and exit_price >= cap * 0.999:
                exit_reason = "TP"
            else:
                exit_reason = "STP"

        ep = entry_price
        sp = float(info.get("signal_price", ep))
        high_se = float(info.get("high_since_entry", ep))
        low_se = float(info.get("low_since_entry", ep))
        mfe_pct, mae_pct = _mfe_mae_pct(side, ep, high_se, low_se)
        hit_3_before_3 = mfe_pct >= 3 and mae_pct > -3
        slippage_entry = ((ep - sp) / sp * 100.0) if sp else 0.0

        if exit_price <= 0 or exit_price != exit_price:
            log_info(
                f"  Final exit: {ticker} invalid exit price {exit_price!r} - "
                "not writing trades.csv (reconcile from IB); clearing state"
            )
            positions_today.discard(ticker)
            try:
                if tp1_trade and getattr(getattr(tp1_trade, "orderStatus", None), "status", "") != "Filled":
                    ib.cancelOrder(tp1_trade.order)
            except Exception:
                pass
            del pending_trades[ticker]
            return

        log_trade(
            date_str,
            ticker,
            side,
            info["entry_time"],
            entry_price,
            shares,
            float(info.get("runner_target") or info.get("target", 0)),
            float(info.get("stop", 0)),
            exit_time,
            exit_price,
            pnl_d,
            pnl_pct,
        )
        log_trade_outcome(
            date_str,
            ticker,
            side,
            info["entry_time"],
            exit_time,
            ep,
            exit_price,
            sp,
            shares,
            float(info.get("runner_target") or info.get("target", 0)),
            float(info.get("stop", 0)),
            pnl_d,
            pnl_pct,
            exit_reason,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            hit_3_before_3=hit_3_before_3,
            slippage_entry_pct=slippage_entry,
            slippage_exit_pct=0,
        )

        trades_exited_today += 1
        if pnl_d > 0:
            win_count_today += 1
        else:
            loss_count_today += 1
        total_pnl_today += pnl_d
        positions_today.discard(ticker)
        # Best-effort: cancel any leftover TP1 order if runner exited first.
        try:
            if tp1_trade and getattr(getattr(tp1_trade, "orderStatus", None), "status", "") != "Filled":
                ib.cancelOrder(tp1_trade.order)
        except Exception:
            pass
        del pending_trades[ticker]
        score = float(info.get("score") or 0.0)
        log_info(f"TRADE | {ticker} | score={score:.2f} | pnl={pnl_d:.2f}")
        log_info(f"  Filled exit: {ticker} {side} @ {exit_price:.2f} PnL ${pnl_d:.2f} ({pnl_pct:.2f}%)")

    def on_exec_details(trade, fill):
        try:
            ticker = trade.contract.symbol
            status = getattr(trade.orderStatus, "status", None)
            if status != "Filled":
                return

            action = getattr(trade.order, "action", None)
            filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
            avg_price = float(actual_fill_price_from_ib(trade, fill) or 0)

            # Entry fill
            if ticker in pending_entry_trades:
                entry_side = (pending_entry_trades[ticker].get("side") or "LONG").upper()
                entry_action = "BUY" if entry_side == "LONG" else "SELL"
                if action == entry_action:
                    if avg_price <= 0:
                        return
                    place_bracket_on_entry_fill(ticker, filled, avg_price)
                    return

            # Exit fill
            if ticker in pending_trades:
                side = (pending_trades[ticker].get("side") or "LONG").upper()
                exit_action = "SELL" if side == "LONG" else "BUY"
                if action == exit_action:
                    _handle_exit_fill(
                        ticker,
                        avg_price,
                        filled_qty=filled,
                        order_id=getattr(trade.order, "orderId", None),
                    )
        except Exception as e:
            log_info(f"  execDetails handler error: {e}")

    def on_order_status(trade):
        try:
            status = getattr(trade.orderStatus, "status", None)
            ticker = trade.contract.symbol
            action = getattr(trade.order, "action", None)
            filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
            avg_price = float(getattr(trade.orderStatus, "avgFillPrice", 0) or 0)

            if status == "Filled":
                if ticker in pending_entry_trades:
                    entry_side = (pending_entry_trades[ticker].get("side") or "LONG").upper()
                    entry_action = "BUY" if entry_side == "LONG" else "SELL"
                    if action == entry_action:
                        place_bracket_on_entry_fill(ticker, filled, avg_price)
                        return

                if ticker in pending_trades:
                    side = (pending_trades[ticker].get("side") or "LONG").upper()
                    exit_action = "SELL" if side == "LONG" else "BUY"
                    if action == exit_action:
                        _handle_exit_fill(
                            ticker,
                            avg_price,
                            filled_qty=filled,
                            order_id=getattr(trade.order, "orderId", None),
                        )
                return

            # Pending entry ended without a usable fill: release slot and do not mark ticker traded.
            if ticker in pending_entry_trades:
                entry_terminal_unfilled = status in (
                    "Cancelled",
                    "ApiCancelled",
                    "Inactive",
                    "ValidationError",
                    "Rejected",
                )
                if entry_terminal_unfilled and filled <= 0:
                    _release_pending_entry_unfilled(ticker, trade, f"order_status_{status}")
        except Exception as e:
            log_info(f"  orderStatus handler error: {e}")

    ib.execDetailsEvent += on_exec_details
    ib.orderStatusEvent += on_order_status

    def _safe_cancel_entry_order(ticker: str, trade) -> None:
        """ib_insync Trade has no .cancel — use IB.cancelOrder (see session.log errors)."""
        try:
            o = getattr(trade, "order", None)
            if o is not None:
                ib.cancelOrder(o)
        except Exception as e:
            log_info(f"  Cancel order {ticker}: {e}")

    def _process_signals_impl():
        nonlocal n_signals_today, signals_this_bar, peak_capital, trades_filled_today, entry_orders_placed, same_ticker_reentry_day_blocks, same_ticker_side_reentry_day_blocks, fast_track_direct_trades, controlled_entry_blocks, entry_strength_blocks, late_entry_blocks
        exit_reason = "complete"
        funnel = {
            "raw": 0,
            "ranked_sorted": 0,
            "filtered_count": 0,
            "final": 0,
            "fallback_to_1": False,
            "min_score_used": None,
            "orders_attempted": 0,
            "orders_placed": 0,
            "blocks": {},  # reason -> count
        }

        def _block(reason: str, n: int = 1) -> None:
            funnel["blocks"][reason] = funnel["blocks"].get(reason, 0) + n

        def _blocks_fmt() -> str:
            if not funnel["blocks"]:
                return "{}"
            parts = [f"{k}={v}" for k, v in sorted(funnel["blocks"].items())]
            return "{" + ", ".join(parts) + "}"

        try:
            now = now_et()
            if not signals_this_bar:
                exit_reason = "no_signals_this_bar"
                log_info("FUNNEL early return: no signals_this_bar")
                return

            log_info(
                f"FUNNEL start: signals_this_bar={len(signals_this_bar)} "
                f"time={now.strftime('%Y-%m-%d %H:%M:%S')} "
                f"trades_filled_today={trades_filled_today} "
                f"positions_today={len(positions_today)} "
                f"pending_entry_trades={len(pending_entry_trades)} "
                f"total_pnl_today={float(total_pnl_today):.2f}"
            )

            # Hard entry cutoff (configurable; default 14:00 ET).
            ec_h = int(getattr(config, "ENTRY_CUTOFF_HOUR", 14))
            ec_m = int(getattr(config, "ENTRY_CUTOFF_MINUTE", 0))
            if (now.hour, now.minute) >= (ec_h, ec_m):
                exit_reason = "entry_cutoff"
                signals_this_bar.clear()
                log_info(
                    f"FUNNEL early return: entry cutoff reached (now >= {ec_h:02d}:{ec_m:02d} ET)"
                )
                return
            late_block_h = int(getattr(config, "BLOCK_ENTRIES_AFTER_HOUR", 13))
            late_block_m = int(getattr(config, "BLOCK_ENTRIES_AFTER_MINUTE", 0))
            if (now.hour, now.minute) >= (late_block_h, late_block_m):
                _block("late_entry_blocked", len(signals_this_bar))
                late_entry_blocks += len(signals_this_bar)
                for sig in signals_this_bar:
                    side = (sig.get("side") or "LONG").upper()
                    _append_blocked_csv_simple(sig, side, "late_entry_blocked")
                signals_this_bar.clear()
                exit_reason = "late_entry_blocked"
                log_info(
                    f"FUNNEL early return: late entry block reached (now >= {late_block_h:02d}:{late_block_m:02d} ET)"
                )
                return

            # --- ADAPTIVE TIME FILTER ---
            late_day = (now.hour, now.minute) >= (13, 30)
            # Keep baseline threshold scale; do not relax conditions to force midday trade flow.
            size_threshold_scale = 1.0

            # --- SOFT LOSS CONTROL (NO BLOCKS) ---
            loss_size_multiplier = 0.7 if loss_count_today >= 3 else 1.0

            # Timing filter: skip first 15 minutes of regular session (09:30-09:44 ET).
            if (now.hour, now.minute) < (9, 45):
                exit_reason = "first_15_min_gate"
                # Drop any premarket-formed candidates so we don't fire a stale burst at 9:30.
                signals_this_bar.clear()
                log_info("FUNNEL early return: first 15 min gate (before 09:45 ET)")
                return
            # Existing market-open gate remains in place.
            mo_h = int(getattr(config, "MARKET_OPEN_HOUR", 9))
            mo_m = int(getattr(config, "MARKET_OPEN_MINUTE", 30))
            if (now.hour, now.minute) < (mo_h, mo_m):
                exit_reason = "before_market_open"
                signals_this_bar.clear()
                log_info(
                    f"FUNNEL early return: before market open (<{mo_h:02d}:{mo_m:02d} ET)"
                )
                return

            sorted_signals = list(signals_this_bar)
            signals_this_bar.clear()
            funnel["raw"] = len(sorted_signals)

            spy_trend = _compute_spy_trend_from_barbuilder(bar_builder)
            spy_dir = (spy_trend.get("direction") or "NEUTRAL").upper()
            min_trade_score_cfg = float(getattr(config, "MIN_TRADE_SCORE", 55.0) or 55.0)
            post_min_rel_vol = float(getattr(config, "POST_CONFIRM_MIN_REL_VOL", 1.2) or 1.2)
            post_max_vwap_dist = float(getattr(config, "POST_CONFIRM_MAX_VWAP_DIST_PCT", 0.35) or 0.35)
            post_require_spy_align = bool(getattr(config, "POST_CONFIRM_REQUIRE_SPY_ALIGNMENT", True))
            atr_pct_min = float(getattr(config, "ATR_PCT_MIN", 0.0) or 0.0)
            atr_pct_max = float(getattr(config, "ATR_PCT_MAX", 999.0) or 999.0)

            post_quality_signals = []
            allow_fast_track_to_trade = bool(getattr(config, "ALLOW_FAST_TRACK_TO_TRADE", False))
            for sig in sorted_signals:
                side = (sig.get("side") or "LONG").upper()
                if (str(sig.get("confirmation_type") or "") == "fast_track") and not allow_fast_track_to_trade:
                    _block("fast_track_trade_blocked")
                    _append_blocked_csv_simple(sig, side, "fast_track_trade_blocked")
                    continue
                score_ok = float(sig.get("score") or 0.0) >= min_trade_score_cfg
                rel_vol_ok = float(sig.get("rel_vol") or 0.0) >= post_min_rel_vol
                vwap_ok = abs(float(sig.get("dist_vwap_pct") or 0.0)) <= post_max_vwap_dist
                atr_pct_sig = float(sig.get("atr_pct") or 0.0)
                atr_ok = atr_pct_min <= atr_pct_sig <= atr_pct_max
                if post_require_spy_align:
                    spy_align_ok = side == spy_dir and spy_dir in {"LONG", "SHORT"}
                else:
                    spy_align_ok = True
                if score_ok and rel_vol_ok and vwap_ok and atr_ok and spy_align_ok:
                    post_quality_signals.append(sig)
                else:
                    _block("post_confirmation_quality_failed")
                    _append_blocked_csv_simple(sig, side, "post_confirmation_quality_failed")
            sorted_signals = post_quality_signals

            sorted_signals = sorted(
                sorted_signals, key=lambda x: float(x.get("score", 0.0) or 0.0), reverse=True
            )
            funnel["ranked_sorted"] = len(sorted_signals)

            base_min_score = 55.0
            min_score = 70.0 if float(total_pnl_today) < 0 else base_min_score
            funnel["min_score_used"] = float(min_score)
            top_take = int(getattr(config, "TOP_TRADES_TO_TAKE", 3) or 3)
            scores = [float(s.get("score", 0.0) or 0.0) for s in sorted_signals]
            if scores:
                log_info(f"SCORE STATS: max={max(scores):.1f} avg={sum(scores)/len(scores):.1f}")

            for i, sig in enumerate(sorted_signals[:5], 1):
                tkr = sig.get("ticker", "?")
                side = (sig.get("side") or "LONG").upper()
                sc = float(sig.get("score", 0.0) or 0.0)
                atr_pct = float(sig.get("atr_pct") or 0.0)
                rel_vol = float(sig.get("rel_vol") or 0.0)
                bias_strength = float(sig.get("bias_strength") or 0.0)
                pct_1d = float(sig.get("pct_change_1d") or 0.0)
                log_info(
                    f"FUNNEL top5 [{i}/5]: {tkr} {side} score={sc:.1f} rank={i} "
                    f"atr_pct={atr_pct:.2f} rel_vol={rel_vol:.2f} "
                    f"bias_strength={bias_strength:.3f} pct_change_1d={pct_1d:.2f}"
                )

            filtered = [
                s for s in sorted_signals if float(s.get("score", 0.0) or 0.0) >= min_score
            ]
            funnel["filtered_count"] = len(filtered)
            # fallback: if nothing qualifies, take best 1
            funnel["fallback_to_1"] = False
            if not filtered and sorted_signals:
                filtered = sorted_signals[:1]
                funnel["fallback_to_1"] = True
            ranked = filtered[: max(1, top_take)]
            funnel["final"] = len(ranked)

            log_info(
                f"FUNNEL score filter: min_score_used={min_score:.1f} "
                f"filtered_count={funnel['filtered_count']} "
                f"fallback_to_1={funnel['fallback_to_1']} "
                f"final_ranked_count={funnel['final']} top_take={top_take}"
            )

            min_sig_trade = int(getattr(config, "MIN_SIGNALS_TO_TRADE", 1) or 1)
            if funnel["final"] < min_sig_trade:
                log_info(
                    f"FUNNEL note: MIN_SIGNALS_TO_TRADE unmet (final={funnel['final']} < {min_sig_trade}); "
                    f"live path does not gate on this"
                )

            if len(ranked) == 0:
                exit_reason = "ranked_empty"
                log_info("FUNNEL early return: ranked empty after score filter")
                return
            if trades_filled_today >= int(getattr(config, "MAX_TRADES_PER_DAY", 6)):
                exit_reason = "max_trades_per_day"
                log_info("FUNNEL early return: MAX_TRADES_PER_DAY")
                return
            if len(positions_today) >= config.MAX_POSITIONS:
                exit_reason = "max_positions"
                log_info("FUNNEL early return: MAX_POSITIONS")
                return

            capital_now = get_account_value(ib, account_id)
            if capital_now <= 0:
                exit_reason = "capital_now_nonpositive"
                log_info(f"FUNNEL early return: capital_now <= 0 ({capital_now:.2f})")
                return
            peak_capital = max(peak_capital, capital_now)
            if start_capital and (start_capital - capital_now) / start_capital >= config.MAX_DAILY_LOSS_PCT:
                exit_reason = "daily_loss_limit"
                log_info("FUNNEL early return: daily loss limit")
                log_info("Max daily loss reached. No new orders.")
                return

            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M:%S")
            regime_state = compute_regime_from_barbuilder(bar_builder)
            regime_mult = (
                float(regime_state.get("size_multiplier", 1.0))
                if getattr(config, "REGIME_ENGINE_ENABLED", True)
                else 1.0
            )
            if getattr(config, "REGIME_ENGINE_ENABLED", True) and getattr(
                config, "REGIME_LOG_VERBOSE", False
            ):
                log_info(
                    f"REGIME SPY {regime_state.get('label')} "
                    f"score={float(regime_state.get('regime_score', 0.0)):.2f} mult={regime_mult:.2f}"
                )

            max_trades_day = int(getattr(config, "MAX_TRADES_PER_DAY", 6))

            def _diag_sizing_numbers(sig_inner: dict, side_inner: str) -> dict:
                """Mirror soft multipliers + dollar math used for orders (late-day is a hard continue, not sm)."""
                sm = 1.0
                if spy_dir != "NEUTRAL" and side_inner != spy_dir:
                    sm *= 0.6
                bias_dir_i = (sig_inner.get("bias_dir") or "NEUTRAL").upper()
                raw_strength_i = float(sig_inner.get("bias_strength") or 0.0)
                weak_bias_threshold_i = 0.10 * size_threshold_scale
                if raw_strength_i < weak_bias_threshold_i:
                    sm *= 0.6
                sm *= loss_size_multiplier
                score_i = float(sig_inner.get("score") or 0)
                if bias_dir_i == "NEUTRAL":
                    eff_i = 1.0
                else:
                    eff_i = 0.75 + 0.25 * min(max(raw_strength_i, 0.0), 1.0)
                risk_m_i = max(0.5, min(1.0, float(sm) * float(regime_mult)))
                entry_price_i = float((sig_inner.get("bar") or {}).get("close") or 0.0)
                capital_for_sizing_i = capital_now * config.MAX_CAPITAL_PCT_USED
                dr_i, _ = size_per_trade(len(ranked), capital_for_sizing_i, entry_price_i)
                df_i = dr_i * eff_i * risk_m_i
                min_d_i = float(getattr(config, "MIN_POSITION_DOLLARS", 1500.0) or 1500.0)
                if 0 < df_i < min_d_i and capital_for_sizing_i >= min_d_i:
                    df_i = min_d_i
                return {
                    "score": score_i,
                    "entry_price": entry_price_i,
                    "capital_now": capital_now,
                    "capital_for_sizing": capital_for_sizing_i,
                    "len_ranked": len(ranked),
                    "bias_strength": raw_strength_i,
                    "effective_strength": eff_i,
                    "size_multiplier": sm,
                    "regime_mult": regime_mult,
                    "dollar_raw": dr_i,
                    "dollar_final": df_i,
                    "min_position_dollars": min_d_i,
                }

            _blocked_csv_path = os.path.join(config.LOG_DIR, "blocked_candidates.csv")
            _blocked_fields = [
                "Date",
                "Time",
                "ticker",
                "side",
                "score",
                "entry_price",
                "capital_now",
                "capital_for_sizing",
                "len_ranked",
                "bias_strength",
                "effective_strength",
                "size_multiplier",
                "regime_mult",
                "dollar_raw",
                "dollar_final",
                "MIN_POSITION_DOLLARS",
                "block_reason",
            ]

            def _log_blocked_candidate(
                block_reason: str,
                sig_inner: dict,
                side_inner: str,
                *,
                overrides: dict | None = None,
            ) -> None:
                dnum = _diag_sizing_numbers(sig_inner, side_inner)
                if overrides:
                    dnum.update(overrides)
                row = {
                    "Date": date_str,
                    "Time": time_str,
                    "ticker": sig_inner.get("ticker", ""),
                    "side": side_inner,
                    "score": f"{dnum['score']:.6f}",
                    "entry_price": f"{dnum['entry_price']:.6f}",
                    "capital_now": f"{dnum['capital_now']:.6f}",
                    "capital_for_sizing": f"{dnum['capital_for_sizing']:.6f}",
                    "len_ranked": str(dnum["len_ranked"]),
                    "bias_strength": f"{dnum['bias_strength']:.6f}",
                    "effective_strength": f"{dnum['effective_strength']:.6f}",
                    "size_multiplier": f"{dnum['size_multiplier']:.6f}",
                    "regime_mult": f"{dnum['regime_mult']:.6f}",
                    "dollar_raw": f"{dnum['dollar_raw']:.6f}",
                    "dollar_final": f"{dnum['dollar_final']:.6f}",
                    "MIN_POSITION_DOLLARS": f"{dnum['min_position_dollars']:.6f}",
                    "block_reason": block_reason,
                }
                try:
                    os.makedirs(config.LOG_DIR, exist_ok=True)
                    file_exists = os.path.isfile(_blocked_csv_path)
                    with open(_blocked_csv_path, "a", newline="", encoding="utf-8") as bf:
                        wr = csv.DictWriter(bf, fieldnames=_blocked_fields)
                        if not file_exists:
                            wr.writeheader()
                        wr.writerow(row)
                except OSError:
                    pass

            for rank_pos, sig in enumerate(ranked, 1):
                if trades_filled_today >= max_trades_day:
                    exit_reason = "max_trades_per_day_loop"
                    _block("max_trades_per_day")
                    log_info("FUNNEL loop stop: MAX_TRADES_PER_DAY")
                    break
                if len(positions_today) + len(pending_entry_trades) >= config.MAX_POSITIONS:
                    exit_reason = "max_positions_loop"
                    _block("max_positions")
                    log_info("FUNNEL loop stop: MAX_POSITIONS (incl pending)")
                    break
                ticker = sig["ticker"]
                side = (sig.get("side") or "LONG").upper()
                low_range_blacklist = {
                    str(s).upper() for s in (getattr(config, "LOW_RANGE_BLACKLIST", []) or [])
                }
                if ticker.upper() in low_range_blacklist:
                    _block("blacklist")
                    _log_blocked_candidate("blacklist", sig, side)
                    log_info(f"BLOCKED: {ticker} (LOW_RANGE_BLACKLIST)")
                    continue
                size_multiplier = 1.0
                if ticker.upper() in ticker_placed_today:
                    _block("same_ticker_reentry_day")
                    same_ticker_reentry_day_blocks += 1
                    if (ticker.upper(), side) in ticker_side_filled_today:
                        same_ticker_side_reentry_day_blocks += 1
                    _log_blocked_candidate("same_ticker_reentry_day", sig, side)
                    log_info(f"BLOCKED: {ticker} {side} (already placed an entry for this ticker today)")
                    continue
                c_close = float((sig.get("bar") or {}).get("close") or 0)
                if not _is_controlled_entry(ticker, side, c_close, sig.get("bar") or {}):
                    controlled_entry_blocks += 1
                    _block("controlled_entry_filter_failed")
                    _log_blocked_candidate("controlled_entry_filter_failed", sig, side)
                    log_info(f"BLOCKED: {ticker} {side} (controlled_entry_filter_failed)")
                    continue
                if ticker in positions_today or ticker in pending_entry_trades:
                    _block("already_in_position_or_pending")
                    _log_blocked_candidate("already_in_position_or_pending", sig, side)
                    log_info(f"BLOCKED: {ticker} (already in positions or pending)")
                    continue

                if side == "SHORT" and not getattr(config, "ENABLE_SHORTS", True):
                    _block("shorts_disabled")
                    _log_blocked_candidate("shorts_disabled", sig, side)
                    log_info(f"BLOCKED: {ticker} {side} (shorts disabled)")
                    continue

                # --- SOFT SPY BIAS FILTER (DO NOT BLOCK TRADES) ---
                if spy_dir != "NEUTRAL" and side != spy_dir:
                    size_multiplier *= 0.6
                    log_info(f"SOFT FILTER: {ticker} {side} against SPY ({spy_dir})")

                # Sizing: NEUTRAL = full size (1.0); LONG/SHORT = scale by bias strength
                bias_dir = (sig.get("bias_dir") or "NEUTRAL").upper()
                raw_strength = float(sig.get("bias_strength") or 0.0)

                # Late day = require stronger conditions (not full block)
                late_day_strength_threshold = 0.35 * size_threshold_scale
                if spy_dir != "NEUTRAL" and late_day and raw_strength < late_day_strength_threshold:
                    _block("late_day_weak_bias")
                    _log_blocked_candidate("late_day_weak_bias", sig, side)
                    log_info(f"BLOCKED: {ticker} {side} (late day weak bias: {raw_strength:.3f})")
                    continue

                # Soft weak-bias handling (no hard block unless near-zero signal quality).
                weak_bias_threshold = 0.10 * size_threshold_scale
                if raw_strength < weak_bias_threshold:
                    size_multiplier *= 0.6
                size_multiplier *= loss_size_multiplier

                # --- VOLATILITY / EXPANSION FILTER ---
                atr_pct = float(sig.get("atr_pct") or 0.0)

                # --- SCORE FILTER (light touch) ---
                score = float(sig.get("score") or 0)
                if score < -15:
                    _block("poor_score")
                    _log_blocked_candidate("poor_score", sig, side)
                    log_info(f"BLOCKED: {ticker} {side} (poor score: {score:.2f})")
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
                        entry_strength_blocks += 1
                        _block("entry_strength_failed")
                        _append_blocked_csv_simple(sig, side, "entry_strength_failed")
                        log_info(f"BLOCKED: {ticker} {side} (entry_strength_failed)")
                        continue

                entry_price = float(sig["bar"]["close"])
                capital_for_sizing = capital_now * config.MAX_CAPITAL_PCT_USED
                dollar_raw_trade, _ = size_per_trade(len(ranked), capital_for_sizing, entry_price)

                # Bias should be a mild confidence modifier, not a direct size multiplier.
                # NEUTRAL = full size.
                # Weak directional bias = modest reduction.
                # Strong directional bias = full size.
                if bias_dir == "NEUTRAL":
                    effective_strength = 1.0
                else:
                    effective_strength = 0.75 + 0.25 * min(max(raw_strength, 0.0), 1.0)

                risk_multiplier = max(0.5, min(1.0, float(size_multiplier) * float(regime_mult)))

                dollar = dollar_raw_trade * effective_strength * risk_multiplier

                # Never allow a qualified trade to shrink below the tradable minimum unless capital itself is insufficient.
                min_position_dollars = float(getattr(config, "MIN_POSITION_DOLLARS", 1500.0) or 1500.0)
                if 0 < dollar < min_position_dollars and capital_for_sizing >= min_position_dollars:
                    dollar = min_position_dollars

                log_info(
                    f"SIZE DIAG: {ticker} {side} score={score:.2f} bias_dir={bias_dir} "
                    f"raw_strength={raw_strength:.4f} effective_strength={effective_strength:.4f} "
                    f"size_multiplier={size_multiplier:.4f} regime_mult={regime_mult:.4f} "
                    f"dollar_raw_trade={dollar_raw_trade:.2f} dollar={dollar:.2f} "
                    f"min_position_dollars={min_position_dollars:.2f}"
                )

                if dollar < min_position_dollars:
                    _block("min_position_dollars")
                    _log_blocked_candidate(
                        "min_position_dollars",
                        sig,
                        side,
                        overrides={"dollar_raw": dollar_raw_trade, "dollar_final": dollar},
                    )
                    log_info(
                        f"BLOCKED: {ticker} {side} (position ${dollar:.0f} < min ${min_position_dollars:.0f})"
                    )
                    continue
                if dollar <= 0 or entry_price <= 0:
                    _block("zero_dollar_or_bad_price")
                    _log_blocked_candidate(
                        "zero_dollar_or_bad_price",
                        sig,
                        side,
                        overrides={"dollar_raw": dollar_raw_trade, "dollar_final": dollar},
                    )
                    log_info(f"BLOCKED: {ticker} {side} (dollar={dollar:.0f} or price=0)")
                    continue
                shares = max(1, int(dollar / entry_price))

                funnel["orders_attempted"] += 1
                try:
                    # Marketable limit orders to cap slippage.
                    limit_price = round(entry_price * (1.001 if side == "LONG" else 0.999), 2)
                    trade = place_marketable_limit_entry_side(
                        ib, ticker, shares, side, limit_price, account_id
                    )
                    placed_at = now_et()
                    pending_entry_trades[ticker] = {
                        "trade": trade,
                        "placed_at": placed_at,
                        "side": side,
                        "bias_dir": sig.get("bias_dir", "NEUTRAL"),
                        "bias_strength": raw_strength,
                        "signal_price": entry_price,
                        "signal_time": time_str,
                        "pct_change_1d": sig.get("pct_change_1d", 0),
                        "rel_vol": sig.get("rel_vol", 0),
                        "atr_pct": sig.get("atr_pct", 0),
                        "dist_vwap_pct": sig.get("dist_vwap_pct", 0),
                        "score": sig.get("score", 0),
                        "rank_position": rank_pos,
                        "shares": shares,
                        "target": entry_price * (1 + config.TARGET_PCT)
                        if side == "LONG"
                        else entry_price * (1 - config.TARGET_PCT),
                        "stop": entry_price * (1 - config.STOP_PCT)
                        if side == "LONG"
                        else entry_price * (1 + config.STOP_PCT),
                    }
                    entry_orders_placed += 1
                    if (str(sig.get("confirmation_type") or "")).lower() == "fast_track":
                        fast_track_direct_trades += 1
                    log_signal(
                        date_str,
                        time_str,
                        ticker,
                        side,
                        sig.get("bias_dir", "NEUTRAL"),
                        raw_strength,
                        sig.get("pct_change_1d", 0),
                        sig.get("rel_vol", 0),
                        sig.get("atr_pct", 0),
                        sig.get("dist_vwap_pct", 0),
                        sig.get("score", 0),
                        rank_pos,
                        entry_price,
                        filled=False,
                    )
                    n_signals_today += 1
                    funnel["orders_placed"] += 1
                    log_info(f"  Signal: {ticker} {side} @ {entry_price:.2f} x {shares} (pending fill)")
                except Exception as e:
                    _block("order_failed")
                    _log_blocked_candidate(f"order_failed:{e!r}", sig, side)
                    log_info(f"  Order failed {ticker}: {e}")

        finally:
            ms = funnel["min_score_used"]
            ms_part = f"{float(ms):.1f}" if ms is not None else "n/a"
            log_info(
                "FUNNEL SUMMARY: "
                f"raw={funnel['raw']} ranked={funnel['ranked_sorted']} "
                f"filtered={funnel['filtered_count']} final={funnel['final']} "
                f"orders_attempted={funnel['orders_attempted']} orders_placed={funnel['orders_placed']} "
                f"exit={exit_reason} min_score={ms_part} blocks={_blocks_fmt()} "
                f"entry_orders_placed={entry_orders_placed} entry_orders_filled={entry_orders_filled} "
                f"entry_orders_unfilled_or_cancelled={entry_orders_unfilled_or_cancelled} "
                f"released_unfilled_entry_orders={released_unfilled_entry_orders} "
                f"same_ticker_reentry_day_blocks={same_ticker_reentry_day_blocks} "
                f"same_ticker_side_reentry_day_blocks={same_ticker_side_reentry_day_blocks}"
            )

    def process_signals():
        if not signals_this_bar:
            return
        try:
            _process_signals_impl()
        except Exception as e:
            log_info(f"process_signals error (session continues): {e}\n{traceback.format_exc()}")

    # --- Subscriptions (ensure ETF is always subscribed) ---
    max_subs = int(getattr(config, "MAX_REALTIME_SUBSCRIPTIONS", 100))
    to_subscribe = list(stream_tickers[:max_subs])
    if etf_symbol not in to_subscribe:
        if len(to_subscribe) >= max_subs:
            to_subscribe = to_subscribe[:-1] + [etf_symbol]
        else:
            to_subscribe.append(etf_symbol)

    log_info(f"Subscribing to 5-sec bars for {len(to_subscribe)} tickers (incl ETF {etf_symbol})...")
    for sym in to_subscribe:
        try:
            bars = ib.reqRealTimeBars(make_stock(sym), 5, "TRADES", False)
            bars.updateEvent += _wrap_rt_bar(sym)
            realtime_streams[sym] = bars
        except Exception:
            pass
        time.sleep(0.05)

    # Initialize bias once we have some ETF bars (will be NEUTRAL until enough closes exist)
    _update_bias_if_due(force=True)

    timeout_sec = int(getattr(config, "ENTRY_ORDER_TIMEOUT_SECONDS", 300))
    close_hour = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_minute = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    eod_close_done = False
    eod_closes_placed: set[str] = set()

    _sd_h = int(getattr(config, "SHUTDOWN_HOUR", 16))
    _sd_m = int(getattr(config, "SHUTDOWN_MINUTE", 0))
    _t0 = now_et()
    log_info(
        f"Entering main loop. US/Eastern now={_t0.strftime('%Y-%m-%d %H:%M:%S')} — "
        f"runs until {_sd_h:02d}:{_sd_m:02d} ET, then report + disconnect. "
        f'Ctrl+C to stop. Create "KILL_SWITCH.txt" to stop safely.'
    )
    if is_after(_sd_h, _sd_m):
        log_info(
            "WARNING: Eastern time is already at/after configured shutdown; this launch will not enter the live loop. "
            "For a full US session, start Arbiter before that time (config SHUTDOWN_HOUR / SHUTDOWN_MINUTE) or the prior calendar evening in your local zone."
        )

    session_exit_reason = "us_eastern_shutdown_reached"
    try:
        while not is_after(config.SHUTDOWN_HOUR, config.SHUTDOWN_MINUTE):
            try:
                if is_kill_switch():
                    log_info("Kill switch detected. Stopping.")
                    session_exit_reason = "kill_switch"
                    break

                _update_bias_if_due(force=False)

                now = now_et()
                if (now.hour, now.minute) >= (close_hour, close_minute):
                    positions = get_all_positions(ib, account_id)
                    for ticker, pos in positions:
                        if ticker in eod_closes_placed:
                            continue
                        if pos == 0:
                            continue
                        try:
                            place_market_close(ib, ticker, account_id)
                            eod_closes_placed.add(ticker)
                            log_info(f"  EOD close: placed market close {ticker} (pos {pos})")
                        except Exception as e:
                            log_info(f"  EOD close failed {ticker}: {e}")
                    if not [p for _, p in positions if p != 0]:
                        eod_close_done = True

                for ticker, info in list(pending_entry_trades.items()):
                    if (now - info["placed_at"]).total_seconds() >= timeout_sec:
                        _safe_cancel_entry_order(ticker, info["trade"])
                        tickers_cancelled_today.add(ticker)
                        _release_pending_entry_unfilled(ticker, info.get("trade"), f"timeout_{timeout_sec}s")

                try:
                    ib.sleep(2)
                except Exception as se:
                    log_info(f"ib.sleep/IB: {se}\n{traceback.format_exc()}")
                    time.sleep(2)

                t = now_et()
                if t.minute % config.BAR_MINUTES == 0 and t.second >= 5 and signals_this_bar:
                    process_signals()
            except Exception as loop_ex:
                log_info(f"Main loop iteration error (continuing): {loop_ex}\n{traceback.format_exc()}")
    except KeyboardInterrupt:
        session_exit_reason = "keyboard_interrupt"
        log_info("Interrupted.")
    except Exception as outer_ex:
        session_exit_reason = f"fatal: {outer_ex!r}"
        log_info(f"Main loop outer error: {outer_ex}\n{traceback.format_exc()}")
    finally:
        log_info(f"Session end reason: {session_exit_reason}")
        _pend = list(pending_entry_trades.keys())
        log_info(
            "SESSION EOD DIAG: "
            f"entry_orders_placed={entry_orders_placed} "
            f"entry_orders_filled={entry_orders_filled} "
            f"entry_orders_unfilled_or_cancelled={entry_orders_unfilled_or_cancelled} "
            f"released_unfilled_entry_orders={released_unfilled_entry_orders} "
            f"same_ticker_reentry_day_blocks={same_ticker_reentry_day_blocks} "
            f"same_ticker_side_reentry_day_blocks={same_ticker_side_reentry_day_blocks} "
            f"pending_entry_trades_count={len(pending_entry_trades)} "
            f"pending_entry_tickers={','.join(sorted(_pend)) if _pend else ''} "
            f"fast_track_direct_trades={fast_track_direct_trades} "
            f"fast_track_setups_stored={fast_track_setups_stored} "
            f"fast_track_setups_confirmed={fast_track_setups_confirmed} "
            f"fast_track_setups_expired={fast_track_setups_expired} "
            f"controlled_entry_blocks={controlled_entry_blocks} "
            f"entry_strength_blocks={entry_strength_blocks} "
            f"late_entry_blocks={late_entry_blocks}"
        )
        capital = get_account_value(ib, account_id)
        peak_capital = max(peak_capital, capital)
        # Best-effort: cancel real-time bar streams created by this run
        for sym, bars in list(realtime_streams.items()):
            try:
                ib.cancelRealTimeBars(bars)
            except Exception:
                pass
        disconnect_ib(ib)
        date_str = now_et().strftime("%Y-%m-%d")
        log_daily_equity(date_str, capital, max(0.0, peak_capital - capital), peak_capital)
        log_daily_regime(
            date_str,
            signals_generated=n_signals_today,
            trades_placed=n_signals_today,
            trades_filled=trades_filled_today,
            trades_exited=trades_exited_today,
            total_pnl=total_pnl_today,
            win_count=win_count_today,
            loss_count=loss_count_today,
        )
        report_path = generate_daily_report(date_str, start_capital, capital, peak_capital)
        if report_path:
            log_info(f"Daily report: {report_path}")
        log_info(f"Disconnected. Session log: {log_path}; trades/equity in {config.LOG_DIR}")


if __name__ == "__main__":
    run()

