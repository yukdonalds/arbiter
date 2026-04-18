# -----------------------------
# Fast Paper Long/Short – Backtest yesterday with 5-sec bars (IBKR)
# -----------------------------
"""
Run: python backtest_fpls_yesterday.py

- Universe: USE_FIXED_UNIVERSE = True → first 100 from S&P list (with metrics from cache).
  False → cached watchlist (top 100).
- Fetches yesterday's 5-sec bars from IBKR for SPY + those tickers.
- Replays bar-by-bar: bias, signals, execution simulation.
- Fill: LONG entry = bar.high + slippage, SHORT entry = bar.low - slippage
  (slippage = random 0.01–0.05, optional spread 0.01).
- Bracket exits: high/low simulation (stop before target in same bar).
- Writes logs/backtest_trades_YYYY-MM-DD.csv and prints summary.
"""

import os
import random
import csv
import time as time_mod
from datetime import datetime, timedelta

import pytz

import config
from ib_connection import connect_ib, make_stock, disconnect_ib
from bar_builder import BarBuilder
from market_bias import get_market_bias_from_closes
from signal_engine import check_v26_bar_side, rank_and_cap
from position_sizing import size_per_trade
from metrics_cache import load_cached_metrics, load_cached_watchlist

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


def _yesterday_et() -> datetime:
    """Last trading day, 16:00 ET (market close)."""
    now = datetime.now(EASTERN)
    d = now.date()
    for _ in range(1, 8):
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            break
    return EASTERN.localize(datetime.combine(d, datetime.min.time().replace(hour=16, minute=0, second=0, microsecond=0)))


def _end_dt_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d %H:%M:%S US/Eastern")


def fetch_5sec_bars_one(ib, symbol: str, end_dt_str: str) -> list[dict]:
    """Return list of {date, open, high, low, close, volume} for 5-sec bars, RTH."""
    try:
        contract = make_stock(symbol)
        bars = ib.reqHistoricalData(
            contract, end_dt_str, "1 D", "5 secs", "TRADES",
            useRTH=True, timeout=60, formatDate=1
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
    # Step back one day until weekday
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return EASTERN.localize(datetime.combine(d, datetime.min.time().replace(hour=16, minute=0, second=0, microsecond=0)))


def load_watchlist_100() -> tuple[list[str], dict]:
    """Cached watchlist and metrics; return (tickers[:100], daily_metrics)."""
    watchlist = load_cached_watchlist()
    metrics = load_cached_metrics()
    if not watchlist or not metrics:
        raise SystemExit("No cached watchlist or metrics. Run live FPLS once to populate cache.")
    tickers = [s for s in watchlist if s in metrics][:100]
    if len(tickers) < 10:
        raise SystemExit("Fewer than 10 tickers in cache; need more for backtest.")
    dm = {s: dict(metrics[s]) for s in tickers if s in metrics}
    return tickers, dm


def load_fixed_universe_100() -> list[str]:
    """Fixed S&P subset: first 100 from sp500_tickers.txt (no cache filtering)."""
    path = getattr(config, "SP500_TICKERS_FILE", os.path.join(config.BASE_DIR, "sp500_tickers.txt"))
    if not os.path.isfile(path):
        raise SystemExit("SP500_TICKERS_FILE not found. Set USE_FIXED_UNIVERSE = False or add file.")
    tickers = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#"):
                tickers.append(s)
    tickers = tickers[:100]
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
    entries_count_by_ticker: dict[str, int] = {}
    last_trade_won_by_ticker: dict[str, bool] = {}
    signals_this_bar = []
    last_closed_bar_key = {}
    capital = start_capital
    trades_out = []
    n_filled = 0
    bias_state = {"direction": "NEUTRAL", "strength": 0.0}
    last_bias_ts = 0.0
    bias_counts = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}
    signal_hits_long = 0
    signal_hits_short = 0
    condition_counts = {"liquidity": 0, "price": 0, "momentum": 0, "volatility": 0, "structural": 0}
    close_hour = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_minute = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    eod_done = False
    last_close: dict[str, float] = {}

    # Partial TP + runner settings (match live FPLS)
    TP1_PCT = float(getattr(config, "PARTIAL_TP_PCT", 0.025))
    TP1_FRACTION = float(getattr(config, "PARTIAL_TP_FRACTION", 0.70))
    RUN_CAP_PCT = float(getattr(config, "RUNNER_CAP_PCT", 0.06))
    TRAIL_BE_MFE = float(getattr(config, "TRAIL_BREAKEVEN_MFE_PCT", 1.5))
    TRAIL_ACT_MFE = float(getattr(config, "TRAIL_ACTIVATE_MFE_PCT", 2.5))
    TRAIL_DIST_PCT = float(getattr(config, "TRAIL_DISTANCE_PCT", 1.5))

    def _shorts_enabled() -> bool:
        return bool(getattr(config, "ALLOW_SHORTS", getattr(config, "ENABLE_SHORTS", True)))

    def _can_reenter_ticker(ticker: str) -> bool:
        allow_reentry = bool(getattr(config, "ALLOW_REENTRY", False))
        if not allow_reentry:
            return entries_count_by_ticker.get(ticker, 0) == 0
        max_re = int(getattr(config, "REENTRY_MAX_PER_TICKER", 1))
        if entries_count_by_ticker.get(ticker, 0) >= max_re:
            return False
        if bool(getattr(config, "REENTRY_ONLY_IF_LAST_TRADE_WIN", False)) and ticker in entries_count_by_ticker:
            return bool(last_trade_won_by_ticker.get(ticker, False))
        return True

    def _is_bias_tradeable(b):
        """Bias = tilt, not permission. Only block shorts when shorts are disabled."""
        d = (b.get("direction") or "NEUTRAL").upper()
        if d == "NEUTRAL":
            return getattr(config, "ALLOW_TRADES_IN_NEUTRAL", True)
        if d == "SHORT" and not _shorts_enabled():
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
            info["current_stop_price"] = ep
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
        side = (info.get("side") or "LONG").upper()
        entry = float(info["entry_price"])
        if side == "SHORT":
            pnl_d = (entry - exit_price) * qty
        else:
            pnl_d = (exit_price - entry) * qty
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
            }
        )
        info["realized_pnl"] = float(info.get("realized_pnl") or 0.0) + float(pnl_d)

    def _check_exits(sym: str, bar_high: float, bar_low: float, t_et: datetime):
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
            last_trade_won_by_ticker[sym] = float(info.get("realized_pnl") or 0.0) > 0.0
            positions_today.discard(sym)
            del pending_trades[sym]

    def _process_signals(t_et):
        nonlocal capital, n_filled
        if not signals_this_bar:
            return
        if (t_et.hour, t_et.minute) >= (close_hour, close_minute):
            signals_this_bar.clear()
            return
        if not _is_bias_tradeable(bias_state):
            signals_this_bar.clear()
            return
        ranked = rank_and_cap(signals_this_bar, config.MAX_SIGNALS_PER_DAY)
        signals_this_bar.clear()
        if len(ranked) < config.MIN_SIGNALS_TO_TRADE:
            return
        if len(positions_today) >= config.MAX_POSITIONS:
            return
        capital_now = capital + sum(t["pnl_dollars"] for t in trades_out)
        if capital_now <= 0:
            return
        size_cap = capital_now * config.MAX_CAPITAL_PCT_USED
        for rank_pos, sig in enumerate(ranked, 1):
            if len(positions_today) >= config.MAX_POSITIONS:
                break
            ticker = sig["ticker"]
            if ticker in positions_today:
                continue
            side = (sig.get("side") or "LONG").upper()
            if side == "SHORT" and not _shorts_enabled():
                continue
            if not _can_reenter_ticker(ticker):
                continue
            bias_dir = (sig.get("bias_dir") or "NEUTRAL").upper()
            raw_strength = float(sig.get("bias_strength") or 0.0)
            effective_strength = 1.0 if bias_dir == "NEUTRAL" else max(raw_strength, 0.01)
            bar = sig["bar"]
            slippage = random.uniform(ENTRY_SLIPPAGE_MIN, ENTRY_SLIPPAGE_MAX)
            if side == "LONG":
                entry_price = float(bar["high"]) + slippage + ENTRY_SPREAD
            else:
                entry_price = float(bar["low"]) - slippage - ENTRY_SPREAD
            if entry_price <= 0:
                continue
            dollar, _ = size_per_trade(len(ranked), size_cap, entry_price)
            dollar *= effective_strength
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

            positions_today.add(ticker)
            n_filled += 1
            pending_trades[ticker] = {
                "side": side,
                "entry_time": t_et.strftime("%H:%M:%S"),
                "entry_price": entry_price,
                "tp1_price": tp1_price,
                "tp2_price": tp2_price,
                "stop_price": stop_price,
                "current_stop_price": stop_price,
                "stop_at_breakeven": False,
                "tp1_remaining": float(tp1_qty),
                "runner_remaining": float(runner_qty),
                "high_since_entry": entry_price,
                "low_since_entry": entry_price,
                "realized_pnl": 0.0,
            }
            entries_count_by_ticker[ticker] = entries_count_by_ticker.get(ticker, 0) + 1

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
        _check_exits(sym, h, l, t_et)
        bar_builder.push_ohlcv(sym, o, h, l, c, vol, t_et)
        minute = t_et.minute
        hour = t_et.hour
        boundary = (hour, minute)
        bias_dir = (bias_state.get("direction") or "NEUTRAL").upper()
        if bias_dir == "NEUTRAL":
            sides_to_generate = ["LONG", "SHORT"] if _shorts_enabled() else ["LONG"]
        else:
            sides_to_generate = [bias_dir]
        raw_bias_strength = float(bias_state.get("strength") or 0)

        if _is_bias_tradeable(bias_state) and sym in daily_metrics and sym not in positions_today:
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
                    if last_closed_bar_key.get(sym) != bar_key:
                        dm = dict(daily_metrics.get(sym, {}))
                        dm["today_volume_so_far"] = sum(bb["volume"] for bb in bar_builder.get_all_closed(sym))
                        bar_dict = {
                            "open": current.get("open"), "high": current.get("high"),
                            "low": current.get("low"), "close": current.get("close"),
                            "volume": current.get("volume", 0),
                        }
                        for side_s in sides_to_generate:
                            eligible, score = check_v26_bar_side(sym, bar_dict, dm, side_s, condition_counts)
                            if eligible:
                                score_weighted = score * _bias_weight(side_s, bias_state)
                                if side_s == "LONG":
                                    signal_hits_long += 1
                                else:
                                    signal_hits_short += 1
                                prev = float(dm.get("prev_close") or c)
                                avg_vol = float(dm.get("avg_vol_20") or 1)
                                today_vol = dm["today_volume_so_far"] + current.get("volume", 0)
                                pct_1d = (c - prev) / prev * 100 if prev else 0
                                rel_vol = (today_vol / avg_vol) if avg_vol else 0
                                atr_pct = float(dm.get("atr_pct") or 0)
                                vwap = (bar_dict["high"] + bar_dict["low"] + c) / 3.0
                                dist_vwap = (c - vwap) / vwap * 100 if vwap else 0
                                signals_this_bar.append({
                                    "ticker": sym, "side": side_s, "bias_dir": bias_dir,
                                    "bias_strength": raw_bias_strength,
                                    "score": score_weighted, "bar": bar_dict,
                                    "pct_change_1d": pct_1d, "rel_vol": rel_vol,
                                    "atr_pct": atr_pct, "dist_vwap_pct": dist_vwap,
                                })
                        last_closed_bar_key[sym] = bar_key

        if minute % BAR_MINUTES == 0 and last_closed_bar_key.get(sym) != boundary:
            last_closed_bar_key[sym] = boundary
            closed = bar_builder.lock_bar(sym, t_et)
            if closed and sym in daily_metrics and sym not in positions_today and _is_bias_tradeable(bias_state):
                dm = dict(daily_metrics.get(sym, {}))
                dm["today_volume_so_far"] = sum(bb["volume"] for bb in bar_builder.get_all_closed(sym)[:-1])
                for side_s in sides_to_generate:
                    eligible, score = check_v26_bar_side(sym, closed, dm, side_s, condition_counts)
                    if eligible:
                        score_weighted = score * _bias_weight(side_s, bias_state)
                        close = float(closed.get("close") or 0)
                        prev = float(dm.get("prev_close") or close)
                        avg_vol = float(dm.get("avg_vol_20") or 1)
                        today_vol = dm["today_volume_so_far"] + closed.get("volume", 0)
                        pct_1d = (close - prev) / prev * 100 if prev else 0
                        rel_vol = (today_vol / avg_vol) if avg_vol else 0
                        atr_pct = float(dm.get("atr_pct") or 0)
                        hc, lc, cc = closed.get("high"), closed.get("low"), close
                        vwap = (hc + lc + cc) / 3.0 if (hc or lc or cc) else 0
                        dist_vwap = (cc - vwap) / vwap * 100 if vwap else 0
                        if side_s == "LONG":
                            signal_hits_long += 1
                        else:
                            signal_hits_short += 1
                        signals_this_bar.append({
                            "ticker": sym, "side": side_s, "bias_dir": bias_dir,
                            "bias_strength": raw_bias_strength,
                            "score": score_weighted, "bar": closed,
                            "pct_change_1d": pct_1d, "rel_vol": rel_vol,
                            "atr_pct": atr_pct, "dist_vwap_pct": dist_vwap,
                        })

        if minute % BAR_MINUTES == 0 and t_et.second >= 5 and signals_this_bar:
            _process_signals(t_et)

    total_pnl = sum(t["pnl_dollars"] for t in trades_out)
    end_capital = start_capital + total_pnl
    diagnostics = {
        "bias_counts": bias_counts,
        "signal_hits_long": signal_hits_long,
        "signal_hits_short": signal_hits_short,
        "condition_counts": condition_counts,
        "events_processed": len(events),
    }
    return trades_out, end_capital, total_pnl, diagnostics


def main():
    date_str = _yesterday_et().strftime("%Y-%m-%d")
    end_dt = _yesterday_et()
    end_dt_str = _end_dt_str(end_dt)
    _p(f"Backtest target date: {date_str} (RTH)")
    use_fixed = getattr(config, "USE_FIXED_UNIVERSE", True)
    if use_fixed:
        tickers = load_fixed_universe_100()
        _p(f"FPLS backtest – {date_str} (5-sec bars, fixed universe, high/low + slippage)")
        _p(f"Universe: first 100 S&P tickers: {len(tickers)} tickers")
    else:
        tickers, daily_metrics = load_watchlist_100()
        _p(f"FPLS backtest – {date_str} (5-sec bars, cached watchlist, high/low + slippage)")
        _p(f"Watchlist: {len(tickers)} tickers from cache")
    _p("Connecting to IB...")
    ib = connect_ib()
    try:
        _p("Connected to IB.")
        # Compute daily metrics from prior trading day close (no lookahead).
        metrics_end_dt = _prior_trading_day_close_et(end_dt)
        metrics_end_dt_str = _end_dt_str(metrics_end_dt)
        daily_metrics = {}
        _p(f"Computing daily metrics ending {metrics_end_dt.strftime('%Y-%m-%d')} close...")
        for i, sym in enumerate(tickers, 1):
            if i == 1:
                _p("Daily-metrics fetch started (this is the slow part).")
            m = fetch_daily_metrics_one(ib, sym, metrics_end_dt_str)
            if m:
                daily_metrics[sym] = m
            if i % 10 == 0 or i == len(tickers):
                _p(f"Metrics {i}/{len(tickers)} (usable: {len(daily_metrics)})")
            time_mod.sleep(0.25)

        tickers = [s for s in tickers if s in daily_metrics]
        if len(tickers) < 10:
            raise SystemExit(f"Too few tickers with metrics ({len(tickers)}).")

        _p(f"Metrics complete. Proceeding with {len(tickers)} tickers.")
        _p("Fetching SPY 5-sec bars...")
        etf_bars = fetch_5sec_bars_one(ib, ETF_SYMBOL, end_dt_str)
        _p(f"SPY: {len(etf_bars)} bars")
        time_mod.sleep(11)
        ticker_bars = {}
        for i, sym in enumerate(tickers):
            if i == 0:
                _p("Ticker 5-sec fetch started (this can take a while).")
            bars = fetch_5sec_bars_one(ib, sym, end_dt_str)
            ticker_bars[sym] = bars
            if (i + 1) % 10 == 0:
                _p(f"Fetched {i + 1}/{len(tickers)} 5-sec series")
            time_mod.sleep(2)
        disconnect_ib(ib)
    except Exception as e:
        disconnect_ib(ib)
        raise SystemExit(f"Fetch failed: {e}")

    events = build_event_stream(etf_bars, ticker_bars)
    _p(f"Event stream: {len(events)} bars")
    if len(events) < 1000:
        raise SystemExit("Too few bars; check date and IB data.")

    start_capital = 100_000.0
    trades, end_capital, total_pnl, diagnostics = run_backtest(events, daily_metrics, start_capital, date_str)

    print("\n--- Diagnostics ---", flush=True)
    print("Bias distribution:", diagnostics["bias_counts"], flush=True)
    print("Long  signal hits:", diagnostics["signal_hits_long"], flush=True)
    print("Short signal hits:", diagnostics["signal_hits_short"], flush=True)
    print("Events processed:", diagnostics["events_processed"], flush=True)
    cc = diagnostics.get("condition_counts", {})
    print("Per-condition hits (which ever fire):", flush=True)
    print("  Liquidity (avg vol):", cc.get("liquidity", 0), flush=True)
    print("  Price:              ", cc.get("price", 0), flush=True)
    print("  Momentum:           ", cc.get("momentum", 0), flush=True)
    print("  Volatility (ATR):   ", cc.get("volatility", 0), flush=True)
    print("  Structural (VWAP):  ", cc.get("structural", 0), flush=True)

    os.makedirs(config.LOG_DIR, exist_ok=True)
    path = os.path.join(config.LOG_DIR, f"backtest_trades_{date_str}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "date", "ticker", "side", "entry_time", "entry_price", "shares", "target", "stop",
            "exit_time", "exit_price", "exit_reason", "pnl_dollars", "pnl_pct"
        ])
        w.writeheader()
        for t in trades:
            w.writerow({k: t.get(k, "") for k in w.fieldnames})
    print(f"\nTrades: {len(trades)}", flush=True)
    print(f"Start capital: ${start_capital:,.2f}", flush=True)
    print(f"End capital:   ${end_capital:,.2f}", flush=True)
    print(f"Total PnL:     ${total_pnl:,.2f}", flush=True)
    if trades:
        wins = sum(1 for t in trades if t["pnl_dollars"] > 0)
        print(f"Wins: {wins}/{len(trades)}", flush=True)
    print(f"Wrote {path}", flush=True)


if __name__ == "__main__":
    main()
