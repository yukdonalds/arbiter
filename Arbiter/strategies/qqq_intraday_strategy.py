"""
QQQ-only intraday strategy: session-trend + VWAP flow continuation.
Separate state from Arbiter stock logic; all orders tagged orderRef QQQ_INTRADAY.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal

import pytz

import config

EASTERN = pytz.timezone("America/New_York")

QQQ_ORDER_REF_DEFAULT = "QQQ_INTRADAY"
SETUP_VWAP = "VWAP_CONTINUATION"
SetupType = Literal["VWAP_CONTINUATION"]


def _cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default)


def _rth_open(ts: datetime) -> datetime:
    h = int(_cfg("RTH_OPEN_HOUR", 9))
    m = int(_cfg("RTH_OPEN_MINUTE", 30))
    d = ts.astimezone(EASTERN).date()
    return EASTERN.localize(datetime.combine(d, datetime.min.time().replace(hour=h, minute=m, second=0, microsecond=0)))


def _in_entry_window(ts: datetime) -> bool:
    t = ts.astimezone(EASTERN)
    sh, sm = int(_cfg("QQQ_ENTRY_START_HOUR", 9)), int(_cfg("QQQ_ENTRY_START_MINUTE", 45))
    eh, em = int(_cfg("QQQ_ENTRY_END_HOUR", 15)), int(_cfg("QQQ_ENTRY_END_MINUTE", 15))
    tt = (t.hour, t.minute)
    return (sh, sm) <= tt <= (eh, em)


def _force_exit_time(ts: datetime) -> bool:
    t = ts.astimezone(EASTERN)
    fh, fm = int(_cfg("QQQ_FORCE_EXIT_HOUR", 15)), int(_cfg("QQQ_FORCE_EXIT_MINUTE", 45))
    return (t.hour, t.minute) >= (fh, fm)


def _wilder_atr_pct(bars: list[dict], period: int = 14) -> float:
    """ATR as fraction of last close (e.g. 0.0025 = 0.25%)."""
    if len(bars) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        hi = float(bars[i]["high"])
        lo = float(bars[i]["low"])
        c_prev = float(bars[i - 1]["close"])
        tr = max(hi - lo, abs(hi - c_prev), abs(lo - c_prev))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for j in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[j]) / period
    close = float(bars[-1]["close"])
    if close <= 0:
        return 0.0
    return atr / close


def _floor_5m_bucket(ts: datetime) -> datetime:
    t = ts.astimezone(EASTERN)
    m = (t.minute // 5) * 5
    return t.replace(minute=m, second=0, microsecond=0)


@dataclass
class QQQPosition:
    side: Literal["LONG", "SHORT"]
    entry_price: float
    shares: float
    stop_price: float
    setup_type: SetupType
    entry_time: datetime
    partial_taken: bool = False
    high_since_entry: float = 0.0
    low_since_entry: float = 0.0
    structural_stop: float = 0.0
    cumulative_realized_pnl: float = 0.0


class QQQIntradayStrategy:
    """
    Stateful QQQ intraday engine for live + backtest.
    Call `on_bar` once per 5-second bar (RTH).
    """

    def __init__(
        self,
        log_fn: Callable[[str], None] | None = None,
        blocked_csv_fn: Callable[[dict], None] | None = None,
    ):
        self.log_fn = log_fn
        self.blocked_csv_fn = blocked_csv_fn
        self._session_date: str | None = None
        self._cum_tp_vol = 0.0
        self._cum_vol = 0.0
        self._session_vwap = 0.0
        self._vwap_hist: deque[tuple[datetime, float]] = deque(maxlen=120)
        self._five_sec_buffer: list[dict] = []
        self._five_min_bars: list[dict] = []
        self._current_5m_bucket: datetime | None = None
        self._partial_5m: dict | None = None
        self._or_high: float | None = None
        self._or_low: float | None = None
        self._or_complete = False
        self._pos: QQQPosition | None = None
        self._trades_today = 0
        self._qqq_loss_streak = 0
        self._account_equity = 100_000.0
        self._session_start_equity = 100_000.0
        self.completed_trades: list[dict] = []
        self._pending_live_entry: dict | None = None
        # Backtest / validation diagnostics (not strategy parameters)
        self._reset_diagnostics()

    def finalize_entry_fill(self, ts: datetime, fill_price: float, qty: float, meta: dict) -> None:
        """Call from IBKR fill handler when defer_entry_fill was used on entry order."""
        if qty <= 0 or fill_price <= 0:
            return
        side = meta.get("side", "LONG").upper()
        setup = meta.get("setup_type", SETUP_VWAP)
        stop = float(meta.get("stop_price") or self._build_stop_entry(side, setup, fill_price))
        self._pos = QQQPosition(
            side=side,
            entry_price=float(fill_price),
            shares=float(qty),
            stop_price=stop,
            setup_type=str(setup),
            entry_time=ts,
            high_since_entry=float(fill_price),
            low_since_entry=float(fill_price),
            structural_stop=stop,
        )
        self._trades_today += 1
        self.qqq_trades_filled += 1
        self._pending_live_entry = None

    def reset_session(self, session_date: str, start_equity: float) -> None:
        self._session_date = session_date
        self._cum_tp_vol = 0.0
        self._cum_vol = 0.0
        self._session_vwap = 0.0
        self._vwap_hist.clear()
        self._five_sec_buffer.clear()
        self._five_min_bars.clear()
        self._current_5m_bucket = None
        self._partial_5m = None
        self._or_high = None
        self._or_low = None
        self._or_complete = False
        self._pos = None
        self._pending_live_entry = None
        self._trades_today = 0
        self._qqq_loss_streak = 0
        self._session_start_equity = float(start_equity)
        self._account_equity = float(start_equity)
        self._reset_diagnostics()

    def _reset_diagnostics(self) -> None:
        self.qqq_bars_received = 0
        self.qqq_regime_pass_count = 0
        self.qqq_regime_fail_count = 0
        self.qqq_vwap_setup_count = 0
        self.qqq_entry_trigger_count = 0
        self.qqq_trades_filled = 0
        self.qqq_trend_up_bars = 0
        self.qqq_trend_down_bars = 0
        self.qqq_chop_bars = 0
        self._regime_fail_reasons: dict[str, int] = defaultdict(int)
        self._time_blocked_evals = 0
        self._last_close = 0.0
        self._last_vwap_dist_pct = 0.0
        self._trend_state = "CHOP"

    def get_session_diagnostics(self) -> dict[str, Any]:
        """End-of-day snapshot for backtest reports (one row per session)."""
        vw = self._session_vwap
        c = float(self._last_close or 0.0)
        vwap_distance_pct = abs(c - vw) / vw * 100.0 if vw > 0 else 0.0
        or_pct = self._or_pct() * 100.0
        atr_pct = self._atr5_pct() * 100.0
        regime_ok_last, rcnt = self._regime_ok(c) if c > 0 else (False, 0)
        fail_reason = self._regime_fail_label(c) if c > 0 and not regime_ok_last else ""

        return {
            "session_date": self._session_date or "",
            "qqq_bars_received": int(self.qqq_bars_received),
            "qqq_regime_pass_count": int(self.qqq_regime_pass_count),
            "qqq_regime_fail_count": int(self.qqq_regime_fail_count),
            "qqq_vwap_setup_count": int(self.qqq_vwap_setup_count),
            "qqq_entry_trigger_count": int(self.qqq_entry_trigger_count),
            "qqq_trades_filled": int(self.qqq_trades_filled),
            "qqq_trade_count": len(self.completed_trades),
            "qqq_trend_up_bars": int(self.qqq_trend_up_bars),
            "qqq_trend_down_bars": int(self.qqq_trend_down_bars),
            "qqq_chop_bars": int(self.qqq_chop_bars),
            "qqq_last_trend_state": str(self._trend_state),
            "opening_range_pct": float(or_pct),
            "qqq_5min_atr_pct": float(atr_pct),
            "vwap_distance_pct": float(vwap_distance_pct),
            "regime_pass_last_bar": bool(regime_ok_last),
            "regime_components_last_bar": int(rcnt),
            "regime_fail_reason_last": fail_reason,
            "regime_fail_reasons_histogram": dict(self._regime_fail_reasons),
            "time_blocked_evals": int(self._time_blocked_evals),
        }

    def set_equity(self, equity: float) -> None:
        self._account_equity = float(equity)

    def _log(self, msg: str) -> None:
        if self.log_fn:
            self.log_fn(msg)

    def _blocked(self, reason: str, detail: str = "") -> None:
        row = {
            "strategy": "QQQ_INTRADAY",
            "block_reason": reason,
            "detail": detail,
            "session_date": self._session_date or "",
        }
        if self.blocked_csv_fn:
            self.blocked_csv_fn(row)
        self._log(f"QQQ_BLOCKED {reason} {detail}".strip())

    def _update_vwap(self, h: float, l_: float, c: float, v: float, ts: datetime) -> None:
        tp = (h + l_ + c) / 3.0 if (h or l_ or c) else c
        w = max(float(v), 1e-9)
        self._cum_tp_vol += tp * w
        self._cum_vol += w
        self._session_vwap = self._cum_tp_vol / self._cum_vol if self._cum_vol > 0 else c
        if len(self._five_sec_buffer) % 60 == 0:
            self._vwap_hist.append((ts, self._session_vwap))

    def _vwap_slope(self) -> float:
        """Normalized slope vs ~5 min ago; 0 if unknown."""
        if len(self._vwap_hist) < 2:
            return 0.0
        old_t, old_v = self._vwap_hist[0]
        _, new_v = self._vwap_hist[-1]
        if old_v <= 0:
            return 0.0
        return (new_v - old_v) / old_v

    def _vwap_slope_20m(self) -> float:
        """Normalized VWAP slope over ~20 minutes (4 x 5-minute snapshots)."""
        if len(self._vwap_hist) < 5:
            return 0.0
        old_v = float(self._vwap_hist[-5][1])
        new_v = float(self._vwap_hist[-1][1])
        if old_v <= 0:
            return 0.0
        return (new_v - old_v) / old_v

    def _finalize_5m_bar(self, bucket: datetime) -> None:
        if not self._partial_5m:
            return
        self._five_min_bars.append(dict(self._partial_5m))
        self._partial_5m = None

    def _push_5sec_to_5m(self, ts: datetime, o: float, h: float, l_: float, c: float, v: float) -> None:
        b = _floor_5m_bucket(ts)
        if self._current_5m_bucket is None:
            self._current_5m_bucket = b
        if b != self._current_5m_bucket:
            self._finalize_5m_bar(self._current_5m_bucket)
            self._current_5m_bucket = b
            self._partial_5m = {
                "time": b,
                "open": o,
                "high": h,
                "low": l_,
                "close": c,
                "volume": v,
            }
        else:
            pm = self._partial_5m
            if pm is None:
                self._partial_5m = {
                    "time": b,
                    "open": o,
                    "high": h,
                    "low": l_,
                    "close": c,
                    "volume": v,
                }
            else:
                pm["high"] = max(float(pm["high"]), h)
                pm["low"] = min(float(pm["low"]), l_)
                pm["close"] = c
                pm["volume"] = float(pm["volume"]) + v

    def _opening_range_tick(self, ts: datetime, h: float, l_: float) -> None:
        open_minutes = int(_cfg("QQQ_OPENING_RANGE_MINUTES", 30))
        rth_open = _rth_open(ts)
        if ts < rth_open:
            return
        mins = (ts - rth_open).total_seconds() / 60.0
        if mins <= open_minutes:
            if self._or_high is None:
                self._or_high, self._or_low = h, l_
            else:
                self._or_high = max(self._or_high, h)
                self._or_low = min(self._or_low, l_)
        elif not self._or_complete:
            self._or_complete = True

    def _or_pct(self) -> float:
        if self._or_high is None or self._or_low is None:
            return 0.0
        mid = (self._or_high + self._or_low) / 2.0
        if mid <= 0:
            return 0.0
        return (self._or_high - self._or_low) / mid

    def _atr5_pct(self) -> float:
        return _wilder_atr_pct(self._five_min_bars, 14) if len(self._five_min_bars) >= 15 else 0.0

    def _regime_ok(self, price: float) -> tuple[bool, int]:
        d = self._regime_components(price)
        return d["regime_ok"], d["components_pass_count"]

    def _regime_components(self, price: float) -> dict[str, Any]:
        atr_ok = self._atr5_pct() >= float(_cfg("QQQ_MIN_5MIN_ATR_PCT", 0.0025))
        or_ok = self._or_pct() >= float(_cfg("QQQ_MIN_OPENING_RANGE_PCT", 0.004))
        vw = self._session_vwap
        dist_ok = vw > 0 and abs(price - vw) / vw >= float(_cfg("QQQ_MIN_VWAP_DISTANCE_PCT", 0.001))
        cnt = int(atr_ok) + int(or_ok) + int(dist_ok)
        regime_ok = cnt >= 2
        return {
            "regime_ok": regime_ok,
            "components_pass_count": cnt,
            "atr_ok": atr_ok,
            "or_ok": or_ok,
            "dist_ok": dist_ok,
            "atr_pct": self._atr5_pct(),
            "or_pct": self._or_pct(),
            "vwap_dist_pct": abs(price - vw) / vw if vw > 0 else 0.0,
        }

    def _regime_fail_label(self, price: float) -> str:
        d = self._regime_components(price)
        if d["regime_ok"]:
            return ""
        failed: list[str] = []
        if not d["atr_ok"]:
            failed.append("atr")
        if not d["or_ok"]:
            failed.append("opening_range")
        if not d["dist_ok"]:
            failed.append("vwap_distance")
        return "need_2of3:" + ",".join(failed) if failed else "need_2of3"

    def _recent_net_move_pct(self, bars: int = 3) -> float:
        if len(self._five_sec_buffer) < bars + 1:
            return 0.0
        seq = self._five_sec_buffer[-(bars + 1):]
        first_close = float(seq[0]["c"])
        last_close = float(seq[-1]["c"])
        if first_close <= 0:
            return 0.0
        return (last_close - first_close) / first_close

    def _vwap_acceptance_ratio(self, bars: int = 5, side: Literal["ABOVE", "BELOW"] = "ABOVE") -> float:
        if len(self._five_sec_buffer) < bars:
            return 0.0
        window = self._five_sec_buffer[-bars:]
        if side == "ABOVE":
            hits = sum(1 for b in window if float(b.get("c") or 0.0) > float(b.get("vw") or 0.0) > 0)
        else:
            hits = sum(1 for b in window if float(b.get("c") or 0.0) < float(b.get("vw") or 0.0) > 0)
        return hits / float(len(window) or 1)

    def _range_expansion_ok(self, idx: int = -1, lookback: int = 10, mult: float = 1.3) -> bool:
        if len(self._five_sec_buffer) < lookback + 1:
            return False
        cur = self._five_sec_buffer[idx]
        cur_range = float(cur.get("h") or 0.0) - float(cur.get("l") or 0.0)
        prev = self._five_sec_buffer[-(lookback + 1) : -1]
        prev_ranges = [(float(b.get("h") or 0.0) - float(b.get("l") or 0.0)) for b in prev]
        if not prev_ranges:
            return False
        avg_prev = sum(prev_ranges) / float(len(prev_ranges))
        return cur_range > mult * avg_prev if avg_prev > 0 else False

    def _vwap_distance_expanding(self, side: Literal["LONG", "SHORT"]) -> bool:
        if len(self._five_sec_buffer) < 2:
            return False
        cur = self._five_sec_buffer[-1]
        prev = self._five_sec_buffer[-2]
        c_now = float(cur.get("c") or 0.0)
        c_prev = float(prev.get("c") or 0.0)
        vw_now = float(cur.get("vw") or 0.0)
        vw_prev = float(prev.get("vw") or 0.0)
        if vw_now <= 0 or vw_prev <= 0:
            return False
        if side == "LONG":
            d_now = (c_now - vw_now) / vw_now
            d_prev = (c_prev - vw_prev) / vw_prev
            return d_now > d_prev
        d_now = (vw_now - c_now) / vw_now
        d_prev = (vw_prev - c_prev) / vw_prev
        return d_now > d_prev

    def _session_return_pct(self) -> float:
        if len(self._five_sec_buffer) < 2:
            return 0.0
        first_close = float(self._five_sec_buffer[0]["c"])
        last_close = float(self._five_sec_buffer[-1]["c"])
        if first_close <= 0:
            return 0.0
        return (last_close - first_close) / first_close

    def _price_vs_vwap_ratio(self, bars: int = 240, side: Literal["ABOVE", "BELOW"] = "ABOVE") -> float:
        if not self._five_sec_buffer:
            return 0.0
        window = self._five_sec_buffer[-min(bars, len(self._five_sec_buffer)) :]
        if not window:
            return 0.0
        if side == "ABOVE":
            hits = sum(1 for b in window if float(b.get("c") or 0.0) > float(b.get("vw") or 0.0) > 0)
        else:
            hits = sum(1 for b in window if float(b.get("c") or 0.0) < float(b.get("vw") or 0.0) > 0)
        return hits / float(len(window))

    def _classify_trend_state(self) -> str:
        slope20 = self._vwap_slope_20m()
        above_ratio = self._price_vs_vwap_ratio(240, "ABOVE")
        below_ratio = self._price_vs_vwap_ratio(240, "BELOW")
        sess_ret = self._session_return_pct()
        up = slope20 > 0 and above_ratio >= 0.70 and sess_ret > 0
        down = slope20 < 0 and below_ratio >= 0.70 and sess_ret < 0
        if up:
            return "TREND_UP"
        if down:
            return "TREND_DOWN"
        return "CHOP"

    def _try_vwap_flow_setup(self, c: float) -> tuple[SetupType | None, Literal["LONG", "SHORT"] | None]:
        if len(self._five_sec_buffer) < 4:
            return None, None
        vw = self._session_vwap
        if vw <= 0:
            return None, None
        slope = self._vwap_slope_20m()
        prev_high = float(self._five_sec_buffer[-2]["h"])
        prev_low = float(self._five_sec_buffer[-2]["l"])
        trend_state = self._classify_trend_state()

        min_vwap_dist = float(_cfg("QQQ_MIN_DISTANCE_FROM_VWAP_PCT", 0.0015))
        vwap_dist = abs(c - vw) / vw if vw > 0 else 0.0
        if vwap_dist < min_vwap_dist:
            return None, None

        # Acceptance logic: require sustained closes on correct VWAP side before continuation entry.
        if trend_state == "TREND_UP" and slope > 0:
            if (
                self._vwap_acceptance_ratio(5, "ABOVE") >= 0.80
                and c > vw
                and c > prev_high
                and self._range_expansion_ok(lookback=10, mult=1.3)
                and self._vwap_distance_expanding("LONG")
            ):
                return SETUP_VWAP, "LONG"
            return None, None

        if trend_state == "TREND_DOWN" and slope < 0:
            if (
                self._vwap_acceptance_ratio(5, "BELOW") >= 0.80
                and c < vw
                and c < prev_low
                and self._range_expansion_ok(lookback=10, mult=1.3)
                and self._vwap_distance_expanding("SHORT")
            ):
                return SETUP_VWAP, "SHORT"
            return None, None

        return None, None

    def _compute_shares(self, entry: float, stop_price: float, side: Literal["LONG", "SHORT"]) -> int:
        eq = self._account_equity
        risk_pct = float(_cfg("QQQ_RISK_PER_TRADE_PCT", 0.005))
        cap_pct = float(_cfg("QQQ_MAX_POSITION_PCT", 0.30))
        smin = float(_cfg("QQQ_STOP_MIN_PCT", 0.003))
        smax = float(_cfg("QQQ_STOP_MAX_PCT", 0.006))
        if entry <= 0:
            return 0
        raw_dist = abs(entry - stop_price) / entry
        stop_pct = max(smin, min(smax, raw_dist))
        risk_dollars = eq * risk_pct
        stop_dollars = entry * stop_pct
        if stop_dollars <= 0:
            return 0
        sh = int(risk_dollars / stop_dollars)
        max_sh = int((eq * cap_pct) / entry)
        return max(0, min(sh, max_sh))

    def _emit_trade(
        self,
        *,
        ts: datetime,
        exit_price: float,
        exit_reason: str,
        shares: float,
        mfe_pct: float,
        mae_pct: float,
    ) -> None:
        if not self._pos:
            return
        p = self._pos
        side = p.side
        ep = p.entry_price
        if side == "LONG":
            pnl_d = (exit_price - ep) * shares
            pnl_pct = ((exit_price / ep - 1.0) * 100.0) if ep else 0.0
        else:
            pnl_d = (ep - exit_price) * shares
            pnl_pct = ((ep - exit_price) / ep * 100.0) if ep else 0.0
        mins = (ts - p.entry_time).total_seconds() / 60.0
        row = {
            "date": ts.astimezone(EASTERN).strftime("%Y-%m-%d"),
            "time": ts.astimezone(EASTERN).strftime("%H:%M:%S"),
            "strategy": "QQQ_INTRADAY",
            "symbol": _cfg("QQQ_SYMBOL", "QQQ"),
            "side": side,
            "setup_type": p.setup_type,
            "entry_price": ep,
            "shares": shares,
            "stop_price": p.stop_price,
            "target_partial_pct": float(_cfg("QQQ_PARTIAL_TP_PCT", 0.005)) * 100.0,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl_dollars": pnl_d,
            "pnl_pct": pnl_pct,
            "MFE_pct": mfe_pct,
            "MAE_pct": mae_pct,
            "time_in_trade_minutes": mins,
        }
        self.completed_trades.append(row)
        if exit_reason == "QQQ_PARTIAL_TP":
            if self._pos:
                self._pos.cumulative_realized_pnl += pnl_d
        elif self._pos:
            tot = self._pos.cumulative_realized_pnl + pnl_d
            if tot < 0:
                self._qqq_loss_streak += 1
            else:
                self._qqq_loss_streak = 0

    def _unrealized_pct(self, price: float) -> float:
        if not self._pos:
            return 0.0
        ep = self._pos.entry_price
        if ep <= 0:
            return 0.0
        if self._pos.side == "LONG":
            return (price - ep) / ep * 100.0
        return (ep - price) / ep * 100.0

    def _maybe_exit_position(self, ts: datetime, h: float, l_: float, c: float) -> list[dict]:
        """Returns action dicts for live (flatten / partial)."""
        actions: list[dict] = []
        if not self._pos:
            return actions
        p = self._pos
        side = p.side
        ep = p.entry_price
        sh = p.shares
        vw = self._session_vwap

        p.high_since_entry = max(p.high_since_entry, h)
        p.low_since_entry = min(p.low_since_entry, l_)

        mfe = (p.high_since_entry - ep) / ep * 100.0 if side == "LONG" else (ep - p.low_since_entry) / ep * 100.0
        mae = (p.low_since_entry - ep) / ep * 100.0 if side == "LONG" else (ep - p.high_since_entry) / ep * 100.0

        # Forced EOD
        if _force_exit_time(ts) and sh > 0:
            self._emit_trade(ts=ts, exit_price=c, exit_reason="QQQ_EOD_EXIT", shares=sh, mfe_pct=mfe, mae_pct=mae)
            actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_EOD_EXIT"})
            self._pos = None
            return actions

        # Partial TP
        tp_pct = float(_cfg("QQQ_PARTIAL_TP_PCT", 0.005)) * 100.0
        frac = float(_cfg("QQQ_PARTIAL_TP_SIZE", 0.50))
        if not p.partial_taken and sh > 1 and self._unrealized_pct(c) >= tp_pct:
            pq = max(1, int(sh * frac))
            pq = min(pq, int(sh) - 1) if sh > 2 else int(sh // 2 or 1)
            self._emit_trade(
                ts=ts,
                exit_price=c,
                exit_reason="QQQ_PARTIAL_TP",
                shares=float(pq),
                mfe_pct=mfe,
                mae_pct=mae,
            )
            actions.append({"type": "close_partial", "side": side, "qty": pq, "reason": "QQQ_PARTIAL_TP"})
            p.partial_taken = True
            p.shares = sh - pq
            rem = p.shares
            trail_pct = float(_cfg("QQQ_TRAIL_DISTANCE_PCT", 0.35))
            if side == "LONG":
                p.structural_stop = max(p.structural_stop, p.high_since_entry * (1.0 - trail_pct / 100.0))
            else:
                p.structural_stop = min(p.structural_stop or ep * 2.0, p.low_since_entry * (1.0 + trail_pct / 100.0))
            sh = rem

        # Hard stop
        if sh > 0:
            if side == "LONG" and l_ <= p.stop_price:
                self._emit_trade(ts=ts, exit_price=p.stop_price, exit_reason="QQQ_STOP", shares=sh, mfe_pct=mfe, mae_pct=mae)
                actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_STOP"})
                self._pos = None
                return actions
            if side == "SHORT" and h >= p.stop_price:
                self._emit_trade(ts=ts, exit_price=p.stop_price, exit_reason="QQQ_STOP", shares=sh, mfe_pct=mfe, mae_pct=mae)
                actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_STOP"})
                self._pos = None
                return actions

        # Structural trail on runner
        if sh > 0 and p.partial_taken:
            trail_pct = float(_cfg("QQQ_TRAIL_DISTANCE_PCT", 0.35))
            if side == "LONG":
                trail = p.high_since_entry * (1.0 - trail_pct / 100.0)
                if l_ <= trail:
                    self._emit_trade(ts=ts, exit_price=trail, exit_reason="QQQ_STRUCTURAL_TRAIL", shares=sh, mfe_pct=mfe, mae_pct=mae)
                    actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_STRUCTURAL_TRAIL"})
                    self._pos = None
                    return actions
            else:
                trail = p.low_since_entry * (1.0 + trail_pct / 100.0)
                if h >= trail:
                    self._emit_trade(ts=ts, exit_price=trail, exit_reason="QQQ_STRUCTURAL_TRAIL", shares=sh, mfe_pct=mfe, mae_pct=mae)
                    actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_STRUCTURAL_TRAIL"})
                    self._pos = None
                    return actions

        # Structural failure (replaces single-bar VWAP fail).
        if sh > 0 and vw > 0 and len(self._five_sec_buffer) >= 2:
            slope20 = self._vwap_slope_20m()
            prev_c = float(self._five_sec_buffer[-2].get("c") or 0.0)
            if side == "LONG":
                if prev_c < vw and c < vw and slope20 < 0:
                    self._emit_trade(
                        ts=ts,
                        exit_price=c,
                        exit_reason="QQQ_STRUCTURAL_FAIL",
                        shares=sh,
                        mfe_pct=mfe,
                        mae_pct=mae,
                    )
                    actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_STRUCTURAL_FAIL"})
                    self._pos = None
                    return actions
            else:
                if prev_c > vw and c > vw and slope20 > 0:
                    self._emit_trade(
                        ts=ts,
                        exit_price=c,
                        exit_reason="QQQ_STRUCTURAL_FAIL",
                        shares=sh,
                        mfe_pct=mfe,
                        mae_pct=mae,
                    )
                    actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_STRUCTURAL_FAIL"})
                    self._pos = None
                    return actions

        # No progress
        np_min = float(_cfg("QQQ_NO_PROGRESS_MINUTES", 30))
        np_pnl = float(_cfg("QQQ_NO_PROGRESS_MIN_PNL_PCT", 0.001)) * 100.0
        if sh > 0:
            open_m = (ts - p.entry_time).total_seconds() / 60.0
            if open_m >= np_min and self._unrealized_pct(c) < np_pnl:
                self._emit_trade(ts=ts, exit_price=c, exit_reason="QQQ_NO_PROGRESS_EXIT", shares=sh, mfe_pct=mfe, mae_pct=mae)
                actions.append({"type": "close", "side": side, "qty": int(sh), "reason": "QQQ_NO_PROGRESS_EXIT"})
                self._pos = None
                return actions

        return actions

    def _can_enter(self, ts: datetime, global_loss_breach: bool) -> tuple[bool, str]:
        if not bool(_cfg("ENABLE_QQQ_INTRADAY", False)):
            return False, "disabled"
        if global_loss_breach:
            return False, "global_loss"
        if _cfg("QQQ_SYMBOL", "QQQ").upper() != "QQQ":
            return False, "symbol"
        if not _in_entry_window(ts):
            return False, "qqq_time_blocked"
        if self._pos:
            return False, "qqq_position_already_open"
        if self._trades_today >= int(_cfg("QQQ_MAX_TRADES_PER_DAY", 3)):
            return False, "qqq_max_trades_reached"
        if self._qqq_loss_streak >= int(_cfg("QQQ_MAX_CONSECUTIVE_LOSSES_PER_DAY", 2)):
            return False, "qqq_max_consecutive_losses"
        return True, ""

    def _build_stop_entry(self, side: Literal["LONG", "SHORT"], setup: SetupType, entry: float) -> float:
        smin = float(_cfg("QQQ_STOP_MIN_PCT", 0.003))
        smax = float(_cfg("QQQ_STOP_MAX_PCT", 0.006))
        if setup == SETUP_VWAP:
            stop = entry * (1.0 - smin) if side == "LONG" else entry * (1.0 + smin)
        else:
            stop = entry * (1.0 - smin) if side == "LONG" else entry * (1.0 + smin)
        raw = abs(entry - stop) / entry if entry > 0 else smin
        adj = max(smin, min(smax, raw))
        if side == "LONG":
            return entry * (1.0 - adj)
        return entry * (1.0 + adj)

    def on_bar(
        self,
        ts: datetime,
        o: float,
        h: float,
        l_: float,
        c: float,
        v: float,
        *,
        global_loss_breach: bool = False,
        backtest: bool = False,
        defer_entry_fill: bool = False,
    ) -> list[dict]:
        """
        Process one RTH 5-second bar. Returns action instructions for live trading.
        """
        actions: list[dict] = []
        sym = _cfg("QQQ_SYMBOL", "QQQ").upper()

        self.qqq_bars_received += 1
        self._last_close = float(c)

        self._update_vwap(h, l_, c, v, ts)
        self._five_sec_buffer.append({"ts": ts, "o": o, "h": h, "l": l_, "c": c, "v": v, "vw": self._session_vwap})
        self._opening_range_tick(ts, h, l_)
        prev_bucket = self._current_5m_bucket
        self._push_5sec_to_5m(ts, o, h, l_, c, v)
        rolled_5m = prev_bucket is not None and self._current_5m_bucket != prev_bucket

        if self._pos:
            actions.extend(self._maybe_exit_position(ts, h, l_, c))
            if self._pos is None:
                return actions

        if not rolled_5m and not backtest:
            return actions

        # Per-bar regime diagnostics (so backtests always show activity even
        # when no setups/entries trigger).
        if (
            self._or_complete
            and self._or_high is not None
            and self._or_low is not None
            and self._session_vwap > 0
            and c > 0
        ):
            regime_ok, _ = self._regime_ok(c)
            if regime_ok:
                self.qqq_regime_pass_count += 1
            else:
                self.qqq_regime_fail_count += 1
                lbl = self._regime_fail_label(c)
                if lbl:
                    self._regime_fail_reasons[lbl] += 1

        self._trend_state = self._classify_trend_state()
        if self._trend_state == "TREND_UP":
            self.qqq_trend_up_bars += 1
        elif self._trend_state == "TREND_DOWN":
            self.qqq_trend_down_bars += 1
        else:
            self.qqq_chop_bars += 1

        setup: SetupType | None = None
        side: Literal["LONG", "SHORT"] | None = None

        setup, side = self._try_vwap_flow_setup(c)
        if setup == SETUP_VWAP:
            self.qqq_vwap_setup_count += 1

        if not setup or not side:
            return actions

        regime_ok, _ = self._regime_ok(c)
        if not regime_ok:
            return actions

        entry = c
        stop = self._build_stop_entry(side, setup, entry)
        dist = abs(entry - stop) / entry if entry > 0 else 0.0
        smin = float(_cfg("QQQ_STOP_MIN_PCT", 0.003))
        smax = float(_cfg("QQQ_STOP_MAX_PCT", 0.006))
        if not (smin - 1e-9 <= dist <= smax + 1e-9):
            self._blocked("qqq_stop_distance_invalid", f"{dist:.5f}")
            return actions

        shares = self._compute_shares(entry, stop, side)
        if shares < 1:
            self._blocked("qqq_size_too_small")
            return actions

        can, reason = self._can_enter(ts, global_loss_breach)
        if not can:
            if reason == "qqq_time_blocked":
                self._time_blocked_evals += 1
            elif reason == "qqq_max_trades_reached":
                self._blocked("qqq_max_trades_reached")
            return actions

        self.qqq_entry_trigger_count += 1

        if defer_entry_fill:
            self._pending_live_entry = {
                "side": side,
                "stop_price": stop,
                "setup_type": setup,
            }
        else:
            self._trades_today += 1
            self.qqq_trades_filled += 1
            self._pos = QQQPosition(
                side=side,
                entry_price=entry,
                shares=float(shares),
                stop_price=stop,
                setup_type=setup,
                entry_time=ts,
                high_since_entry=h,
                low_since_entry=l_,
                structural_stop=stop,
            )
        actions.append(
            {
                "type": "entry",
                "symbol": sym,
                "side": side,
                "qty": shares,
                "stop_price": stop,
                "setup_type": setup,
                "strategy": "QQQ_INTRADAY",
                "defer_entry_fill": defer_entry_fill,
            }
        )
        self._log(
            f"QQQ_INTRADAY ENTRY {setup} {side} px={entry:.2f} sh={shares} stop={stop:.2f} strategy=QQQ_INTRADAY"
        )
        return actions
