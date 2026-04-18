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

import os
import time
import logging
from datetime import datetime, time as dtime

import pytz

import config
from ib_connection import connect_ib, get_account_value, get_position_signed, get_all_positions, disconnect_ib, make_stock
from bar_builder import BarBuilder
from market_bias import get_market_bias_from_closes
from signal_engine import check_v26_bar_side, rank_and_cap
from position_sizing import size_per_trade
from order_execution import (
    place_market_entry_side,
    place_bracket_exits_side,
    place_market_close,
    place_partial_runner_exits_side,
    place_stop_order_side,
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

    log_info("Fast Paper Long/Short – Startup")
    tickers = load_tickers()
    if not tickers:
        log_info("No tickers in sp500_tickers.txt.")
        return
    log_info(f"Loaded {len(tickers)} tickers from list")

    ib = connect_ib()
    account_id = ib.managedAccounts()[0] if ib.managedAccounts() else ""
    ib.reqMarketDataType(getattr(config, "MARKET_DATA_TYPE", 3))
    ib.sleep(2)
    capital = get_account_value(ib, account_id)
    log_info(f"Connected. Account: {account_id}, NetLiquidation: ${capital:,.2f}")

    init_trade_log()
    init_equity_log()

    # --- Watchlist + metrics (same cache-first startup as momentum version) ---
    stream_tickers = None
    daily_metrics: dict = {}
    if is_cache_fresh():
        cached = load_cached_metrics()
        watchlist = load_cached_watchlist()
        if cached and watchlist:
            stream_tickers = [s for s in watchlist if s in cached][: getattr(config, "WATCHLIST_TOP_N", 100)]
            daily_metrics = {s: dict(cached[s]) for s in stream_tickers if s in cached}
            log_info(f"Using cached watchlist and metrics: {len(stream_tickers)} tickers (instant)")

    if not stream_tickers or len(daily_metrics) < 50:
        if getattr(config, "USE_EXTERNAL_SCREEN", False):
            result = build_watchlist_external(top_n=getattr(config, "WATCHLIST_TOP_N", 100))
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
            watchlist, daily_metrics = build_watchlist_parallel(
                ib, tickers, top_n=getattr(config, "WATCHLIST_TOP_N", 100)
            )
            stream_tickers = watchlist
            if daily_metrics:
                save_cached_metrics(daily_metrics)
                save_cached_watchlist(stream_tickers)
                log_info(f"Saved cache for next run. Using {len(stream_tickers)} tickers (IB parallel).")

    if not stream_tickers:
        stream_tickers = tickers[: min(getattr(config, "WATCHLIST_TOP_N", 100), len(tickers))]
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
    start_capital = capital
    peak_capital = capital

    n_signals_today = 0
    trades_filled_today = 0
    trades_exited_today = 0
    win_count_today = 0
    loss_count_today = 0
    total_pnl_today = 0.0

    pending_entry_trades: dict = {}  # ticker -> {"trade": trade, "placed_at": datetime, "side": "LONG"/"SHORT", ...}
    pending_trades: dict = {}  # ticker -> active trade info (incl side)
    tickers_cancelled_today: set[str] = set()
    entries_count_by_ticker: dict[str, int] = {}
    last_trade_won_by_ticker: dict[str, bool] = {}
    last_closed_bar_key: dict[str, tuple[int, int]] = {}
    signals_this_bar: list[dict] = []
    realtime_streams: dict[str, object] = {}

    # Bias state (updated on schedule from ETF bars)
    bias_state = {"direction": "NEUTRAL", "strength": 0.0}
    last_bias_update_ts = 0.0

    etf_symbol = getattr(config, "ETF_SYMBOL", "SPY").upper()
    min_bias_strength = float(getattr(config, "MIN_BIAS_STRENGTH", 0.6))
    bias_refresh_interval = float(getattr(config, "BIAS_REFRESH_INTERVAL", 300))

    def _shorts_enabled() -> bool:
        return bool(getattr(config, "ALLOW_SHORTS", getattr(config, "ENABLE_SHORTS", True)))

    def _can_reenter_ticker(ticker: str) -> bool:
        """Allow re-entries only when configured and recent outcome supports it."""
        allow_reentry = bool(getattr(config, "ALLOW_REENTRY", False))
        if not allow_reentry:
            return entries_count_by_ticker.get(ticker, 0) == 0
        max_re = int(getattr(config, "REENTRY_MAX_PER_TICKER", 1))
        if entries_count_by_ticker.get(ticker, 0) >= max_re:
            return False
        if bool(getattr(config, "REENTRY_ONLY_IF_LAST_TRADE_WIN", False)) and ticker in entries_count_by_ticker:
            return bool(last_trade_won_by_ticker.get(ticker, False))
        return True

    def _is_bias_tradeable(b: dict) -> bool:
        """Bias = tilt, not permission. Only block shorts when ENABLE_SHORTS is False."""
        d = (b.get("direction") or "NEUTRAL").upper()
        if d == "NEUTRAL":
            return getattr(config, "ALLOW_TRADES_IN_NEUTRAL", True)
        if d == "SHORT" and not _shorts_enabled():
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

    def _update_bias_if_due(force: bool = False) -> None:
        nonlocal last_bias_update_ts, bias_state
        now_ts = time.time()
        if not force and (now_ts - last_bias_update_ts) < bias_refresh_interval:
            return
        bias_state = _compute_bias_from_barbuilder(bar_builder)
        last_bias_update_ts = now_ts
        log_info(f"Bias: {bias_state.get('direction')} ({float(bias_state.get('strength') or 0):.2f}) via {etf_symbol}")

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

    def on_5sec_bar(sym, bars_list, has_new_bar):
        nonlocal signals_this_bar
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

            trail_be_mfe = float(getattr(config, "TRAIL_BREAKEVEN_MFE_PCT", 2.0))
            trail_act_mfe = float(getattr(config, "TRAIL_ACTIVATE_MFE_PCT", 3.0))
            trail_dist_pct = float(getattr(config, "TRAIL_DISTANCE_PCT", 1.5))
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

                    high_se = float(info.get("high_since_entry", ep))
                    low_se = float(info.get("low_since_entry", ep))
                    mfe_pct, _ = _mfe_mae_pct(side, ep, high_se, low_se)

                    if mfe_pct >= trail_be_mfe and not info.get("stop_at_breakeven"):
                        ib.cancelOrder(stop_trade.order)
                        new_sl = place_stop_order_side(ib, sym, shares, ep, side, account_id)
                        info["stop_trade"] = new_sl
                        info["current_stop_price"] = ep
                        info["stop_at_breakeven"] = True
                        log_info(f"  Trail: {sym} {side} stop moved to breakeven @ {ep:.2f}")
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

        t = bar.date if hasattr(bar, "date") else now_et()
        if hasattr(t, "tzinfo") and t.tzinfo is None:
            t = EASTERN.localize(t)
        bar_builder.push(sym, float(bar.close), float(bar.volume), t)

        minute = t.minute
        hour = t.hour
        boundary = (hour, minute)

        # Signal generation uses current bias (updated on schedule in the main loop)
        if not _is_bias_tradeable(bias_state):
            return
        bias_dir = (bias_state.get("direction") or "NEUTRAL").upper()
        if bias_dir == "NEUTRAL":
            sides_to_generate = ["LONG", "SHORT"] if _shorts_enabled() else ["LONG"]
        else:
            sides_to_generate = [bias_dir]
        raw_bias_strength = float(bias_state.get("strength") or 0.0)

        # Intrabar
        intrabar_min_age = getattr(config, "INTRABAR_MIN_AGE_SECONDS", 60)
        current = bar_builder.get_current_bar(sym)
        if (
            current
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
                if last_closed_bar_key.get(sym) != bar_key:
                    dm = dict(daily_metrics.get(sym, {}))
                    dm["today_volume_so_far"] = sum(b["volume"] for b in bar_builder.get_all_closed(sym))
                    bar_dict = {
                        "open": current.get("open"),
                        "high": current.get("high"),
                        "low": current.get("low"),
                        "close": current.get("close"),
                        "volume": current.get("volume", 0),
                    }
                    for side_s in sides_to_generate:
                        eligible, score = check_v26_bar_side(sym, bar_dict, dm, side_s)
                        if eligible:
                            score_weighted = score * _bias_weight(side_s, bias_state)
                            close = float(bar_dict.get("close") or 0)
                            prev = float(dm.get("prev_close") or close)
                            avg_vol = float(dm.get("avg_vol_20") or 1)
                            today_vol = float(dm.get("today_volume_so_far", 0) + float(bar_dict.get("volume") or 0))
                            print(f"{sym} avg_vol_20={avg_vol} today_vol={today_vol}")
                            pct_change_1d = (close - prev) / prev * 100 if prev else 0
                            rel_vol = (today_vol / avg_vol) if avg_vol else 0
                            atr_pct = float(dm.get("atr_pct") or 0)
                            h, l_, c = float(bar_dict.get("high") or close), float(bar_dict.get("low") or close), close
                            vwap = (h + l_ + c) / 3.0 if (h or l_ or c) else 0
                            dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                            signals_this_bar.append(
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
                                    "dist_vwap_pct": dist_vwap,
                                }
                            )
                    last_closed_bar_key[sym] = bar_key

        # Bar close
        if minute % config.BAR_MINUTES == 0 and last_closed_bar_key.get(sym) != boundary:
            last_closed_bar_key[sym] = boundary
            closed = bar_builder.lock_bar(sym, t)
            if not closed or sym in positions_today or sym in pending_entry_trades or sym not in daily_metrics:
                return
            dm = dict(daily_metrics.get(sym, {}))
            dm["today_volume_so_far"] = sum(b["volume"] for b in bar_builder.get_all_closed(sym)[:-1])
            for side_s in sides_to_generate:
                eligible, score = check_v26_bar_side(sym, closed, dm, side_s)
                if eligible:
                    score_weighted = score * _bias_weight(side_s, bias_state)
                    close = float(closed.get("close") or 0)
                    prev = float(dm.get("prev_close") or close)
                    avg_vol = float(dm.get("avg_vol_20") or 1)
                    today_vol = float(dm.get("today_volume_so_far", 0) + float(closed.get("volume") or 0))
                    print(f"{sym} avg_vol_20={avg_vol} today_vol={today_vol}")
                    pct_change_1d = (close - prev) / prev * 100 if prev else 0
                    rel_vol = (today_vol / avg_vol) if avg_vol else 0
                    atr_pct = float(dm.get("atr_pct") or 0)
                    h, l_, c = float(closed.get("high") or close), float(closed.get("low") or close), close
                    vwap = (h + l_ + c) / 3.0 if (h or l_ or c) else 0
                    dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                    signals_this_bar.append(
                        {
                            "ticker": sym,
                            "side": side_s,
                            "bias_dir": bias_dir,
                            "bias_strength": raw_bias_strength,
                            "score": score_weighted,
                            "bar": closed,
                            "pct_change_1d": pct_change_1d,
                            "rel_vol": rel_vol,
                            "atr_pct": atr_pct,
                            "dist_vwap_pct": dist_vwap,
                        }
                    )

    def place_bracket_on_entry_fill(ticker: str, filled: float, avg_price: float, entry_info: dict | None = None) -> bool:
        nonlocal trades_filled_today
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
            exits = place_partial_runner_exits_side(ib, ticker, filled, float(avg_price), side, account_id)
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
            "realized_pnl": 0.0,
        }
        entries_count_by_ticker[ticker] = entries_count_by_ticker.get(ticker, 0) + 1
        positions_today.add(ticker)
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
            info["realized_pnl"] = float(info.get("realized_pnl") or 0.0) + float(pnl_d)
            if pnl_d > 0:
                win_count_today += 1
            else:
                loss_count_today += 1
            log_info(f"  Filled TP1: {ticker} {side} @ {exit_price:.2f} x {qty:.0f} PnL ${pnl_d:.2f} ({pnl_pct:.2f}%)")
            # Reduce remaining TP1 shares so we don't double count.
            info["tp1_shares"] = max(0.0, float(info.get("tp1_shares") or 0.0) - qty)
            pending_trades[ticker] = info
            return

        side = (info.get("side") or "LONG").upper()
        exit_time = now_et().strftime("%H:%M:%S")
        date_str = now_et().strftime("%Y-%m-%d")

        entry_price = float(info["entry_price"])
        shares = float(filled_qty or info["shares"])
        if side == "SHORT":
            pnl_d = (entry_price - exit_price) * shares
            pnl_pct = ((entry_price - exit_price) / entry_price * 100.0) if entry_price else 0.0
            cap = float(info.get("runner_target") or info.get("target") or 0.0)
            exit_reason = "TP" if (cap and exit_price <= cap * 1.001) else "STP"
        else:
            pnl_d = (exit_price - entry_price) * shares
            pnl_pct = ((exit_price / entry_price - 1) * 100.0) if entry_price else 0.0
            cap = float(info.get("runner_target") or info.get("target") or 0.0)
            exit_reason = "TP" if (cap and exit_price >= cap * 0.999) else "STP"

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
        info["realized_pnl"] = float(info.get("realized_pnl") or 0.0) + float(pnl_d)
        last_trade_won_by_ticker[ticker] = float(info.get("realized_pnl") or 0.0) > 0.0
        positions_today.discard(ticker)
        # Best-effort: cancel any leftover TP1 order if runner exited first.
        try:
            if tp1_trade and getattr(getattr(tp1_trade, "orderStatus", None), "status", "") != "Filled":
                ib.cancelOrder(tp1_trade.order)
        except Exception:
            pass
        del pending_trades[ticker]
        log_info(f"  Filled exit: {ticker} {side} @ {exit_price:.2f} PnL ${pnl_d:.2f} ({pnl_pct:.2f}%)")

    def on_exec_details(trade, fill):
        try:
            ticker = trade.contract.symbol
            status = getattr(trade.orderStatus, "status", None)
            if status != "Filled":
                return

            action = getattr(trade.order, "action", None)
            filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
            avg_price = float(
                getattr(trade.orderStatus, "avgFillPrice", 0)
                or getattr(getattr(fill, "execution", None), "price", 0)
                or 0
            )

            # Entry fill
            if ticker in pending_entry_trades:
                entry_side = (pending_entry_trades[ticker].get("side") or "LONG").upper()
                entry_action = "BUY" if entry_side == "LONG" else "SELL"
                if action == entry_action:
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
            if status != "Filled":
                return
            ticker = trade.contract.symbol
            action = getattr(trade.order, "action", None)
            filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
            avg_price = float(getattr(trade.orderStatus, "avgFillPrice", 0) or 0)

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
        except Exception as e:
            log_info(f"  orderStatus handler error: {e}")

    ib.execDetailsEvent += on_exec_details
    ib.orderStatusEvent += on_order_status

    def process_signals():
        nonlocal n_signals_today, signals_this_bar
        if not signals_this_bar:
            return
        now = now_et()
        # Hard gate: never place entry orders before market open.
        mo_h = int(getattr(config, "MARKET_OPEN_HOUR", 9))
        mo_m = int(getattr(config, "MARKET_OPEN_MINUTE", 30))
        if (now.hour, now.minute) < (mo_h, mo_m):
            # Drop any premarket-formed candidates so we don't fire a stale burst at 9:30.
            signals_this_bar.clear()
            return
        close_hour = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
        close_minute = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
        if (now.hour, now.minute) >= (close_hour, close_minute):
            return

        ranked = rank_and_cap(signals_this_bar, config.MAX_SIGNALS_PER_DAY)
        signals_this_bar.clear()

        ranked = sorted(ranked, key=lambda s: s["ticker"] in tickers_cancelled_today)
        if len(ranked) < config.MIN_SIGNALS_TO_TRADE:
            return
        if len(positions_today) >= config.MAX_POSITIONS:
            return

        capital_now = get_account_value(ib, account_id)
        if capital_now <= 0:
            return
        if start_capital and (start_capital - capital_now) / start_capital >= config.MAX_DAILY_LOSS_PCT:
            log_info("Max daily loss reached. No new orders.")
            return

        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        for rank_pos, sig in enumerate(ranked, 1):
            if len(positions_today) + len(pending_entry_trades) >= config.MAX_POSITIONS:
                break
            ticker = sig["ticker"]
            if ticker in positions_today or ticker in pending_entry_trades:
                log_info(f"BLOCKED: {ticker} (already in positions or pending)")
                continue

            side = (sig.get("side") or "LONG").upper()
            if side == "SHORT" and not _shorts_enabled():
                log_info(f"BLOCKED: {ticker} {side} (shorts disabled)")
                continue
            if not _can_reenter_ticker(ticker):
                log_info(f"BLOCKED: {ticker} {side} (re-entry rules)")
                continue

            # Sizing: NEUTRAL = full size (1.0); LONG/SHORT = scale by bias strength
            bias_dir = (sig.get("bias_dir") or "NEUTRAL").upper()
            raw_strength = float(sig.get("bias_strength") or 0.0)
            effective_strength = 1.0 if bias_dir == "NEUTRAL" else max(raw_strength, 0.01)

            entry_price = float(sig["bar"]["close"])
            capital_for_sizing = capital_now * config.MAX_CAPITAL_PCT_USED
            dollar, _ = size_per_trade(len(ranked), capital_for_sizing, entry_price)
            dollar *= effective_strength
            if dollar <= 0 or entry_price <= 0:
                log_info(f"BLOCKED: {ticker} {side} (dollar={dollar:.0f} or price=0)")
                continue
            shares = max(1, int(dollar / entry_price))

            try:
                trade = place_market_entry_side(ib, ticker, shares, side, account_id)
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
                    "target": entry_price * (1 + config.TARGET_PCT) if side == "LONG" else entry_price * (1 - config.TARGET_PCT),
                    "stop": entry_price * (1 - config.STOP_PCT) if side == "LONG" else entry_price * (1 + config.STOP_PCT),
                }
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
                log_info(f"  Signal: {ticker} {side} @ {entry_price:.2f} x {shares} (pending fill)")
            except Exception as e:
                log_info(f"  Order failed {ticker}: {e}")

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
            bars.updateEvent += lambda b, hasNew, s=sym: on_5sec_bar(s, b, hasNew)
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

    log_info('Entering main loop. Ctrl+C to stop. Create "KILL_SWITCH.txt" to stop safely.')
    try:
        while not is_after(config.SHUTDOWN_HOUR, config.SHUTDOWN_MINUTE):
            if is_kill_switch():
                log_info("Kill switch detected. Stopping.")
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
                    try:
                        info["trade"].cancel()
                    except Exception as e:
                        log_info(f"  Cancel order {ticker}: {e}")
                    tickers_cancelled_today.add(ticker)
                    del pending_entry_trades[ticker]
                    log_info(f"  Entry order timeout ({timeout_sec}s): cancelled {ticker}, can retry later (bumped down)")

            ib.sleep(2)
            t = now_et()
            if t.minute % config.BAR_MINUTES == 0 and t.second >= 5 and signals_this_bar:
                process_signals()
    except KeyboardInterrupt:
        log_info("Interrupted.")
    finally:
        capital = get_account_value(ib, account_id)
        # Best-effort: cancel real-time bar streams created by this run
        for sym, bars in list(realtime_streams.items()):
            try:
                ib.cancelRealTimeBars(bars)
            except Exception:
                pass
        disconnect_ib(ib)
        date_str = now_et().strftime("%Y-%m-%d")
        log_daily_equity(date_str, capital, peak_capital - capital, peak_capital)
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

