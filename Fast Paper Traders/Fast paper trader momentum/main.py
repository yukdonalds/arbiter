# -----------------------------
# Fast Paper Trader – Main (cache-first startup, parallel scan fallback)
# -----------------------------
"""
Run: python main.py
Uses cached daily metrics when fresh (~instant startup). Otherwise runs a parallel
scan of full list (config.FAST_SCAN_MAX_TICKERS), top WATCHLIST_TOP_N for real-time
instead of ~1 hour. All data and scripts live in this folder.
"""
import os
import time
import logging
from datetime import datetime, timedelta, time as dtime
import pytz
from ib_insync import IB, util

import config
from ib_connection import connect_ib, get_account_value, get_position_size, get_all_positions, disconnect_ib, make_stock
from bar_builder import BarBuilder
from signal_engine import check_v26_bar, rank_and_cap
from position_sizing import size_per_trade
from order_execution import place_market_entry, place_bracket_exits, place_market_sell, place_stop_order
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
    out = []
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


def run():
    # Session log: document what the run does
    log_path = getattr(config, "SESSION_LOG_FILE", os.path.join(config.LOG_DIR, "session.log"))
    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(log_fmt)
    file_handler.setLevel(logging.INFO)
    app_log = logging.getLogger("fast_paper")
    app_log.setLevel(logging.INFO)
    app_log.handlers.clear()
    app_log.addHandler(file_handler)
    app_log.propagate = False

    def log_info(msg: str) -> None:
        app_log.info(msg)
        print(msg)

    log_info("Fast Paper Trader – Startup")
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

    # --- Fast path: use cache if fresh ---
    stream_tickers = None
    daily_metrics = {}
    if is_cache_fresh():
        cached = load_cached_metrics()
        watchlist = load_cached_watchlist()
        if cached and watchlist:
            stream_tickers = [s for s in watchlist if s in cached][: getattr(config, "WATCHLIST_TOP_N", 100)]
            daily_metrics = {s: dict(cached[s]) for s in stream_tickers if s in cached}
            log_info(f"Using cached watchlist and metrics: {len(stream_tickers)} tickers (instant)")

    # --- Fallback: external screen (full S&P 500, no IB) then IB parallel ---
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
            watchlist, daily_metrics = build_watchlist_parallel(ib, tickers, top_n=getattr(config, "WATCHLIST_TOP_N", 100))
            stream_tickers = watchlist
            if daily_metrics:
                save_cached_metrics(daily_metrics)
                save_cached_watchlist(stream_tickers)
                log_info(f"Saved cache for next run. Using {len(stream_tickers)} tickers (IB parallel).")

    if not stream_tickers:
        stream_tickers = tickers[: min(getattr(config, "WATCHLIST_TOP_N", 100), len(tickers))]
        log_info(f"Fallback: using first {len(stream_tickers)} tickers (no metrics yet)")
    if not daily_metrics:
        # Minimal metrics so we don't KeyError; signal checks will filter
        daily_metrics = {s: {"avg_vol_20": 0, "atr_pct": 0, "prev_close": 0, "today_volume_so_far": 0} for s in stream_tickers}

    init_signal_log()
    init_trade_outcomes_log()
    init_daily_regime_log()
    bar_builder = BarBuilder()
    positions_today = set()
    open_orders = {}
    start_capital = capital
    peak_capital = capital
    n_signals_today = 0
    trades_filled_today = 0
    trades_exited_today = 0
    win_count_today = 0
    loss_count_today = 0
    total_pnl_today = 0.0
    pending_entry_trades = {}  # ticker -> {"trade": trade, "placed_at": datetime}
    tickers_cancelled_today = set()  # unfilled orders we cancelled; ticker can retry (bumped down)
    pending_trades = {}
    realtime_bars = {}
    last_closed_15m = {}
    signals_this_bar = []

    def on_5sec_bar(sym, bars_list, has_new_bar):
        nonlocal signals_this_bar
        if not has_new_bar or not bars_list:
            return
        bar = bars_list[-1]
        # Update MFE/MAE for open positions
        if sym in pending_trades:
            info = pending_trades[sym]
            ep = info.get("entry_price") or 0
            if ep > 0:
                high = getattr(bar, "high", None) or getattr(bar, "close", ep)
                low = getattr(bar, "low", None) or getattr(bar, "close", ep)
                info["high_since_entry"] = max(info.get("high_since_entry", ep), high)
                info["low_since_entry"] = min(info.get("low_since_entry", ep), low)
            # Trailing stop: breakeven then trail below high
            trail_be_mfe = getattr(config, "TRAIL_BREAKEVEN_MFE_PCT", 2.0)
            trail_act_mfe = getattr(config, "TRAIL_ACTIVATE_MFE_PCT", 3.0)
            trail_dist_pct = getattr(config, "TRAIL_DISTANCE_PCT", 1.5)
            stop_trade = info.get("stop_trade")
            if stop_trade and ep > 0:
                high_se = info.get("high_since_entry", ep)
                mfe_pct = (high_se - ep) / ep * 100
                try:
                    # Only replace stop if we still have a long position (avoid double-sell → short)
                    pos = get_position_size(ib, account_id, sym)
                    if pos >= info.get("shares", 0):
                        # Move stop to breakeven when MFE >= threshold
                        if mfe_pct >= trail_be_mfe and not info.get("stop_at_breakeven"):
                            ib.cancelOrder(stop_trade.order)
                            new_sl = place_stop_order(ib, sym, info["shares"], ep, account_id)
                            info["stop_trade"] = new_sl
                            info["current_stop_price"] = ep
                            info["stop_at_breakeven"] = True
                            log_info(f"  Trail: {sym} stop moved to breakeven @ {ep:.2f}")
                        # Trail stop below high when MFE >= trail-activate threshold
                        elif mfe_pct >= trail_act_mfe:
                            new_stop = high_se * (1 - trail_dist_pct / 100.0)
                            current_stop = info.get("current_stop_price") or 0
                            if new_stop > current_stop and new_stop > ep:
                                ib.cancelOrder(stop_trade.order)
                                new_sl = place_stop_order(ib, sym, info["shares"], new_stop, account_id)
                                info["stop_trade"] = new_sl
                                info["current_stop_price"] = new_stop
                                log_info(f"  Trail: {sym} stop moved to {new_stop:.2f}")
                except Exception as e:
                    log_info(f"  Trail stop update failed {sym}: {e}")
        t = bar.date if hasattr(bar, "date") else now_et() if hasattr(bar, "date") else now_et()
        if hasattr(t, "tzinfo") and t.tzinfo is None:
            t = EASTERN.localize(t) if hasattr(t, "hour") else now_et()
        elif not hasattr(t, "hour"):
            t = now_et()
        bar_builder.push(sym, bar.close, bar.volume, t)
        minute = t.minute if hasattr(t, "minute") else now_et().minute
        hour = t.hour if hasattr(t, "hour") else now_et().hour
        boundary = (hour, minute)

        # Intrabar: fire when current bar has built for INTRABAR_MIN_AGE_SECONDS and passes v26 (enter earlier)
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
                if hasattr(start_et, "timestamp") and hasattr(t, "timestamp"):
                    age_sec = (t.timestamp() - start_et.timestamp())
                else:
                    age_sec = (t - start_et).total_seconds() if hasattr(t, "__sub__") else 0
            except Exception:
                age_sec = 0
            if age_sec >= intrabar_min_age:
                # Bar-close boundary for this bar (so we don't fire again at close)
                sm = start_et.minute if hasattr(start_et, "minute") else minute
                sh = start_et.hour if hasattr(start_et, "hour") else hour
                next_close_m = ((sm // config.BAR_MINUTES) + 1) * config.BAR_MINUTES
                bar_close_h = sh + (next_close_m // 60)
                bar_close_m = next_close_m % 60
                bar_boundary = (bar_close_h, bar_close_m)
                if last_closed_15m.get(sym) != bar_boundary:
                    dm = dict(daily_metrics.get(sym, {}))
                    dm["today_volume_so_far"] = sum(
                        b["volume"] for b in bar_builder.get_all_closed(sym)
                    )
                    bar_dict = {
                        "open": current.get("open"),
                        "high": current.get("high"),
                        "low": current.get("low"),
                        "close": current.get("close"),
                        "volume": current.get("volume", 0),
                    }
                    eligible, score = check_v26_bar(sym, bar_dict, dm)
                    if eligible:
                        close = bar_dict.get("close") or 0
                        prev = dm.get("prev_close") or close
                        avg_vol = dm.get("avg_vol_20") or 1
                        today_vol = dm.get("today_volume_so_far", 0) + bar_dict.get("volume", 0)
                        pct_change_1d = (close - prev) / prev * 100 if prev else 0
                        rel_vol = (today_vol / avg_vol) if avg_vol else 0
                        atr_pct = dm.get("atr_pct") or 0
                        h, l_, c = bar_dict.get("high", close), bar_dict.get("low", close), close
                        vwap = (h + l_ + c) / 3.0 if (h or l_ or c) else 0
                        dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                        signals_this_bar.append({
                            "ticker": sym, "score": score, "bar": bar_dict,
                            "pct_change_1d": pct_change_1d, "rel_vol": rel_vol,
                            "atr_pct": atr_pct, "dist_vwap_pct": dist_vwap,
                        })
                        last_closed_15m[sym] = bar_boundary

        # Bar close: fire when bar closes (if not already fired intrabar)
        if minute % config.BAR_MINUTES == 0 and last_closed_15m.get(sym) != boundary:
            last_closed_15m[sym] = boundary
            closed = bar_builder.lock_bar(sym, t)
            if not closed or sym in positions_today or sym in pending_entry_trades or sym not in daily_metrics:
                return
            dm = daily_metrics.get(sym, {})
            dm = dict(dm)
            dm["today_volume_so_far"] = sum(
                b["volume"] for b in bar_builder.get_all_closed(sym)[:-1]
            )
            eligible, score = check_v26_bar(sym, closed, dm)
            if eligible:
                close = closed.get("close") or 0
                prev = dm.get("prev_close") or close
                avg_vol = dm.get("avg_vol_20") or 1
                today_vol = dm.get("today_volume_so_far", 0) + closed.get("volume", 0)
                pct_change_1d = (close - prev) / prev * 100 if prev else 0
                rel_vol = (today_vol / avg_vol) if avg_vol else 0
                atr_pct = dm.get("atr_pct") or 0
                h, l_, c = closed.get("high", close), closed.get("low", close), close
                vwap = (h + l_ + c) / 3.0 if (h or l_ or c) else 0
                dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                signals_this_bar.append({
                    "ticker": sym, "score": score, "bar": closed,
                    "pct_change_1d": pct_change_1d, "rel_vol": rel_vol,
                    "atr_pct": atr_pct, "dist_vwap_pct": dist_vwap,
                })

    def place_bracket_on_entry_fill(ticker: str, filled: float, avg_price: float, entry_info: dict | None = None) -> bool:
        if filled <= 0 or avg_price <= 0:
            return False
        # Prevent double bracket when fill event fires twice (execDetails + orderStatus)
        if ticker in pending_trades:
            return True
        # Reserve slot so a concurrent fill callback cannot place a second bracket
        pending_trades[ticker] = {"_placing_bracket": True}
        info = pending_entry_trades.pop(ticker, {}) if ticker in pending_entry_trades else {}
        if entry_info is None:
            entry_info = info
        try:
            tp_price, stop_price, sl_trade = place_bracket_exits(ib, ticker, filled, float(avg_price), account_id)
        except Exception as e:
            log_info(f"  Bracket placement failed {ticker}: {e}")
            del pending_trades[ticker]
            return False
        entry_time = now_et().strftime("%H:%M:%S")
        signal_price = entry_info.get("signal_price", avg_price)
        slippage_pct = (float(avg_price) - signal_price) / signal_price * 100 if signal_price else 0
        # Log signal with fill info
        log_signal(
            now_et().strftime("%Y-%m-%d"), entry_time, ticker,
            entry_info.get("pct_change_1d", 0), entry_info.get("rel_vol", 0),
            entry_info.get("atr_pct", 0), entry_info.get("dist_vwap_pct", 0),
            entry_info.get("score", 0), entry_info.get("rank_position", 0),
            signal_price, filled=True, fill_price=avg_price, fill_time=entry_time,
            slippage_pct=slippage_pct,
            time_to_fill_sec=(now_et() - entry_info.get("placed_at", now_et())).total_seconds() if entry_info.get("placed_at") else 0,
        )
        pending_trades[ticker] = {
            "entry_time": entry_time,
            "entry_price": float(avg_price),
            "shares": filled,
            "target": tp_price,
            "stop": stop_price,
            "current_stop_price": stop_price,
            "stop_trade": sl_trade,
            "signal_price": signal_price,
            "pct_change_1d": entry_info.get("pct_change_1d", 0),
            "rel_vol": entry_info.get("rel_vol", 0),
            "atr_pct": entry_info.get("atr_pct", 0),
            "dist_vwap_pct": entry_info.get("dist_vwap_pct", 0),
            "score": entry_info.get("score", 0),
            "rank_position": entry_info.get("rank_position", 0),
            "high_since_entry": float(avg_price),
            "low_since_entry": float(avg_price),
        }
        positions_today.add(ticker)
        nonlocal trades_filled_today
        trades_filled_today += 1
        log_info(f"  Filled entry: {ticker} @ {avg_price:.2f} x {filled} -> bracket TP={tp_price:.2f} STP={stop_price:.2f}")
        return True

    def on_exec_details(trade, fill):
        try:
            ticker = trade.contract.symbol
            status = getattr(trade.orderStatus, "status", None)
            if status != "Filled":
                return
            if getattr(trade.order, "action", None) == "BUY":
                filled = getattr(trade.orderStatus, "filled", 0) or 0
                avg_price = getattr(trade.orderStatus, "avgFillPrice", 0) or getattr(
                    getattr(fill, "execution", None), "price", 0
                )
                if place_bracket_on_entry_fill(ticker, filled, float(avg_price)):
                    return
            if getattr(trade.order, "action", None) == "SELL":
                exit_price = float(
                    getattr(trade.orderStatus, "avgFillPrice", None)
                    or getattr(getattr(fill, "execution", None), "price", 0)
                    or 0
                )
                _handle_sell_fill(ticker, exit_price)
        except Exception as e:
            log_info(f"  execDetails handler error: {e}")

    def _handle_sell_fill(ticker: str, exit_price: float):
        """Shared exit logic for execDetails and orderStatus (TP/SL fills)."""
        if ticker not in pending_trades:
            return
        info = pending_trades[ticker]
        if info.get("_placing_bracket"):
            return
        exit_time = now_et().strftime("%H:%M:%S")
        date_str = now_et().strftime("%Y-%m-%d")
        pnl_d = (exit_price - info["entry_price"]) * info["shares"]
        pnl_pct = (exit_price / info["entry_price"] - 1) * 100
        ep = info["entry_price"]
        sp = info.get("signal_price", ep)
        mfe_pct = (info.get("high_since_entry", ep) - ep) / ep * 100 if ep else 0
        mae_pct = (info.get("low_since_entry", ep) - ep) / ep * 100 if ep else 0
        hit_3_before_3 = mfe_pct >= 3 and mae_pct > -3
        slippage_entry = (ep - sp) / sp * 100 if sp else 0
        exit_reason = "TP" if exit_price >= info.get("target", exit_price) * 0.999 else "STP"
        log_trade(
            date_str, ticker, info["entry_time"], info["entry_price"], info["shares"],
            info["target"], info["stop"], exit_time, exit_price, pnl_d, pnl_pct,
        )
        log_trade_outcome(
            date_str, ticker, info["entry_time"], exit_time, ep, exit_price, sp,
            info["shares"], info.get("target", 0), info.get("stop", 0),
            pnl_d, pnl_pct, exit_reason,
            mfe_pct=mfe_pct, mae_pct=mae_pct, hit_3_before_3=hit_3_before_3,
            slippage_entry_pct=slippage_entry, slippage_exit_pct=0,
        )
        nonlocal trades_exited_today, win_count_today, loss_count_today, total_pnl_today
        trades_exited_today += 1
        if pnl_d > 0:
            win_count_today += 1
        else:
            loss_count_today += 1
        total_pnl_today += pnl_d
        positions_today.discard(ticker)
        del pending_trades[ticker]
        log_info(f"  Filled exit: {ticker} @ {exit_price:.2f} PnL ${pnl_d:.2f} ({pnl_pct:.2f}%)")

    def on_order_status(trade):
        """Fallback for BUY fills (execDetails can miss). Also handle SELL fills (GAT/TP/SL) when execDetails misses."""
        try:
            status = getattr(trade.orderStatus, "status", None)
            if status != "Filled":
                return
            ticker = trade.contract.symbol
            filled = getattr(trade.orderStatus, "filled", 0) or 0
            avg_price = getattr(trade.orderStatus, "avgFillPrice", 0) or 0
            if getattr(trade.order, "action", None) == "BUY":
                place_bracket_on_entry_fill(ticker, filled, float(avg_price))
            elif getattr(trade.order, "action", None) == "SELL":
                _handle_sell_fill(ticker, float(avg_price))
        except Exception as e:
            log_info(f"  orderStatus handler error: {e}")

    ib.execDetailsEvent += on_exec_details
    ib.orderStatusEvent += on_order_status

    def process_signals():
        nonlocal n_signals_today, capital, peak_capital, signals_this_bar
        if not signals_this_bar:
            return
        # No new entries at or after EOD close time (avoid reopening positions right after close)
        now = now_et()
        if (now.hour, now.minute) >= (close_hour, close_minute):
            return
        ranked = rank_and_cap(signals_this_bar, config.MAX_SIGNALS_PER_DAY)
        signals_this_bar.clear()
        # Bump tickers that had a cancelled order today to end of list (can retry, but after first-time candidates)
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
        capital_for_sizing = capital_now * config.MAX_CAPITAL_PCT_USED
        date_str = now_et().strftime("%Y-%m-%d")
        time_str = now_et().strftime("%H:%M:%S")
        for rank_pos, sig in enumerate(ranked, 1):
            if len(positions_today) + len(pending_entry_trades) >= config.MAX_POSITIONS:
                break
            ticker = sig["ticker"]
            if ticker in positions_today or ticker in pending_entry_trades:
                continue
            entry_price = sig["bar"]["close"]
            dollar, _ = size_per_trade(len(ranked), capital_for_sizing, entry_price)
            if dollar <= 0 or entry_price <= 0:
                continue
            shares = max(1, int(dollar / entry_price))
            try:
                trade = place_market_entry(ib, ticker, shares, account_id)
                placed_at = now_et()
                pending_entry_trades[ticker] = {
                    "trade": trade, "placed_at": placed_at,
                    "signal_price": entry_price, "signal_time": time_str,
                    "pct_change_1d": sig.get("pct_change_1d", 0), "rel_vol": sig.get("rel_vol", 0),
                    "atr_pct": sig.get("atr_pct", 0), "dist_vwap_pct": sig.get("dist_vwap_pct", 0),
                    "score": sig.get("score", 0), "rank_position": rank_pos,
                    "shares": shares, "target": entry_price * (1 + config.TARGET_PCT),
                    "stop": entry_price * (1 - config.STOP_PCT),
                }
                log_signal(
                    date_str, time_str, ticker,
                    sig.get("pct_change_1d", 0), sig.get("rel_vol", 0),
                    sig.get("atr_pct", 0), sig.get("dist_vwap_pct", 0),
                    sig.get("score", 0), rank_pos, entry_price, filled=False,
                )
                n_signals_today += 1
                log_info(f"  Signal: {ticker} @ {entry_price:.2f} x {shares} (pending fill)")
            except Exception as e:
                log_info(f"  Order failed {ticker}: {e}")

    max_subs = getattr(config, "MAX_REALTIME_SUBSCRIPTIONS", 100)
    to_subscribe = stream_tickers[:max_subs]
    # Log subscribed list so we can check later if a ticker was in the top 100
    try:
        ts = now_et().strftime("%Y-%m-%d_%H%M")
        subscribed_path = os.path.join(config.LOG_DIR, f"subscribed_{ts}.txt")
        with open(subscribed_path, "w", encoding="utf-8") as f:
            for s in to_subscribe:
                f.write(s + "\n")
        log_info(f"Subscribed tickers (top {len(to_subscribe)}) logged to {subscribed_path}")
    except Exception as e:
        log_info(f"Could not write subscribed list: {e}")
    if len(stream_tickers) > max_subs:
        log_info(f"Subscribing to 5-sec bars for {len(to_subscribe)} tickers (capped from {len(stream_tickers)} by IB limit)...")
    else:
        log_info(f"Subscribing to 5-sec bars for {len(to_subscribe)} tickers...")
    for sym in to_subscribe:
        try:
            bars = ib.reqRealTimeBars(make_stock(sym), 5, "TRADES", False)
            bars.updateEvent += lambda b, hasNew, s=sym: on_5sec_bar(s, b, hasNew)
            realtime_bars[sym] = bars
        except Exception:
            pass
        time.sleep(0.05)

    timeout_sec = getattr(config, "ENTRY_ORDER_TIMEOUT_SECONDS", 300)
    close_hour = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_minute = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    eod_close_done = False
    eod_sells_placed = set()  # tickers we've already placed EOD sell for (avoid duplicate orders)

    log_info("Entering main loop. Ctrl+C to stop. Create KILL_SWITCH.txt to stop safely.")
    try:
        while not is_after(config.SHUTDOWN_HOUR, config.SHUTDOWN_MINUTE):
            if is_kill_switch():
                log_info("Kill switch detected. Stopping.")
                break
            now = now_et()
            # At 3:45 (config) close any open positions so none are held overnight
            if (now.hour, now.minute) >= (close_hour, close_minute):
                # Keep checking until all long positions are closed
                positions = get_all_positions(ib, account_id)
                longs = [(sym, int(p)) for sym, p in positions if p > 0]
                for ticker, shares in longs:
                    if ticker not in eod_sells_placed:
                        try:
                            place_market_sell(ib, ticker, shares, account_id)
                            eod_sells_placed.add(ticker)
                            log_info(f"  EOD close: placed market sell {shares} {ticker}")
                        except Exception as e:
                            log_info(f"  EOD close failed {ticker}: {e}")
                if not longs:
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
            # Process signals any time during bar-close minute (>=5 sec in gives bars time to arrive)
            if t.minute % config.BAR_MINUTES == 0 and t.second >= 5 and signals_this_bar:
                process_signals()
    except KeyboardInterrupt:
        log_info("Interrupted.")
    finally:
        capital = get_account_value(ib, account_id)
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
