# -----------------------------
# Fast Paper Long/Short – Bracket orders (direction-aware)
# -----------------------------
from __future__ import annotations

from datetime import datetime
import pytz
from ib_insync import MarketOrder, Order

import config
from ib_connection import make_stock, get_position_signed, get_all_positions, resolve_account_id

EASTERN = pytz.timezone("America/New_York")


def actual_fill_price_from_ib(trade, fill) -> float:
    """
    Resolve the actual IB average fill price for an entry.
    Priority:
    1) fill.avgFillPrice (when present on Fill)
    2) trade.orderStatus.avgFillPrice
    3) fill.execution.price
    """
    fill_avg = float(getattr(fill, "avgFillPrice", 0) or 0)
    if fill_avg > 0:
        return fill_avg
    status_avg = float(getattr(getattr(trade, "orderStatus", None), "avgFillPrice", 0) or 0)
    if status_avg > 0:
        return status_avg
    exec_price = float(getattr(getattr(fill, "execution", None), "price", 0) or 0)
    return exec_price


def runner_secure_stop_price(entry_price: float, side: str) -> float:
    """
    Return the runner stop price that guarantees net profit after TP1.
    LONG: entry + 0.5%; SHORT: entry - 0.5%.
    """
    side = (side or "").upper()
    buf = float(getattr(config, "RUNNER_SECURE_GAIN_BUFFER_PCT", 0.005))
    if side == "LONG":
        return round(entry_price * (1 + buf), 2)
    if side == "SHORT":
        return round(entry_price * (1 - buf), 2)
    return entry_price


def _eod_time_str(hour: int, minute: int) -> str:
    """IB format: yyyymmdd hh:mm:ss {optional Timezone}"""
    now = datetime.now(EASTERN)
    return f"{now.strftime('%Y%m%d')} {hour:02d}:{minute:02d}:00 US/Eastern"


def place_market_entry_side(ib, ticker: str, shares: float, side: str, account_id: str = ""):
    side = (side or "").upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid side: {side}")
    contract = make_stock(ticker)
    account_id = resolve_account_id(ib, account_id)
    action = "BUY" if side == "LONG" else "SELL"
    order = MarketOrder(action, shares)
    order.account = account_id
    order.tif = "DAY"
    trade = ib.placeOrder(contract, order)
    return trade


def place_marketable_limit_entry_side(
    ib,
    ticker: str,
    shares: float,
    side: str,
    limit_price: float,
    account_id: str = "",
):
    """
    Place a marketable limit entry (for faster fill vs. a plain limit order).
    - LONG: BUY limit at/above signal price
    - SHORT: SELL limit at/below signal price
    """
    side = (side or "").upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid side: {side}")
    contract = make_stock(ticker)
    account_id = resolve_account_id(ib, account_id)
    action = "BUY" if side == "LONG" else "SELL"
    limit_price = round(float(limit_price), 2)

    order = Order()
    order.action = action
    order.orderType = "LMT"
    order.totalQuantity = shares
    order.lmtPrice = limit_price
    order.tif = "DAY"
    order.account = account_id
    trade = ib.placeOrder(contract, order)
    return trade


def place_market_close(ib, ticker: str, account_id: str = "") -> None:
    """Close any open position without flipping (sell longs, buy-to-cover shorts)."""
    account_id = resolve_account_id(ib, account_id)
    pos = get_position_signed(ib, account_id, ticker)
    qty = int(abs(pos))
    if qty <= 0:
        return
    contract = make_stock(ticker)
    action = "SELL" if pos > 0 else "BUY"
    order = MarketOrder(action, qty)
    order.account = account_id
    order.tif = "DAY"
    ib.placeOrder(contract, order)


def place_stop_order_side(ib, ticker: str, shares: float, stop_price: float, side: str, account_id: str = ""):
    """
    Place a single GTD stop used for trailing/breakeven replacement.
    LONG: SELL STP (close long)
    SHORT: BUY STP (close short)
    Never places more than current absolute position.
    """
    side = (side or "").upper()
    if side not in ("LONG", "SHORT"):
        return None
    account_id = resolve_account_id(ib, account_id)
    pos = get_position_signed(ib, account_id, ticker)
    if (side == "LONG" and pos <= 0) or (side == "SHORT" and pos >= 0):
        return None
    close_qty = min(int(abs(pos)), int(shares))
    if close_qty <= 0:
        return None

    contract = make_stock(ticker)
    close_h = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_m = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    good_till = _eod_time_str(close_h, close_m - 1) if close_m > 0 else _eod_time_str(close_h - 1, 59)

    sl = Order()
    sl.action = "SELL" if side == "LONG" else "BUY"
    sl.orderType = "STP"
    sl.totalQuantity = close_qty
    sl.auxPrice = round(stop_price, 2)
    sl.tif = "GTD"
    sl.goodTillDate = good_till
    sl.account = account_id
    trade = ib.placeOrder(contract, sl)
    return trade


def place_bracket_exits_side(ib, ticker: str, shares: float, fill_price: float, side: str, account_id: str = ""):
    """
    Create TP/SL OCA exits sized to the entry shares.
    LONG: TP=SELL LMT above, SL=SELL STP below
    SHORT: TP=BUY  LMT below, SL=BUY  STP above
    """
    side = (side or "").upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid side: {side}")

    contract = make_stock(ticker)
    account_id = resolve_account_id(ib, account_id)
    close_h = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_m = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    gtd = _eod_time_str(close_h, close_m - 1) if close_m > 0 else _eod_time_str(close_h - 1, 59)

    fill_price = float(fill_price or 0.0)
    if fill_price <= 0:
        raise ValueError("fill_price must be > 0 (use actual IB avg fill price)")

    if side == "LONG":
        take_profit_price = round(fill_price * (1 + config.TARGET_PCT), 2)
        stop_price = round(fill_price * (1 - config.STOP_PCT), 2)
        tp_action = sl_action = "SELL"
    else:
        take_profit_price = round(fill_price * (1 - config.TARGET_PCT), 2)
        stop_price = round(fill_price * (1 + config.STOP_PCT), 2)
        tp_action = sl_action = "BUY"

    tp = Order()
    tp.action = tp_action
    tp.orderType = "LMT"
    tp.totalQuantity = shares
    tp.lmtPrice = take_profit_price
    tp.tif = "GTD"
    tp.goodTillDate = gtd

    sl = Order()
    sl.action = sl_action
    sl.orderType = "STP"
    sl.totalQuantity = shares
    sl.auxPrice = stop_price
    sl.tif = "GTD"
    sl.goodTillDate = gtd

    for o in (tp, sl):
        o.account = account_id

    oca_group = f"EOD_{side}_{ticker}_{datetime.now(EASTERN).strftime('%H%M%S')}"
    ib.oneCancelsAll([tp, sl], oca_group, 1)
    ib.placeOrder(contract, tp)
    sl_trade = ib.placeOrder(contract, sl)
    return take_profit_price, stop_price, sl_trade


def place_partial_runner_exits_side(
    ib,
    ticker: str,
    shares: float,
    fill_price: float,
    side: str,
    account_id: str = "",
    atr_pct: float = 0.0,
) -> dict:
    """
    Exits for "partial TP + runner":
    - TP1: ATR-based limit for PARTIAL_TP_FRACTION of shares
    - Runner: OCA between TP2 (ATR-based) and SL (ATR-based) for remaining shares.
      When trail logic moves runner stop to "breakeven", use runner_secure_stop_price()
      so runner is guaranteed net winner (entry±0.5%).

    Returns dict with prices, quantities, and trades (tp1_trade, tp2_trade, sl_trade).
    """
    side = (side or "").upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid side: {side}")
    fill_price = float(fill_price or 0.0)
    if fill_price <= 0:
        raise ValueError("fill_price must be > 0 (use actual IB avg fill price)")

    contract = make_stock(ticker)
    account_id = resolve_account_id(ib, account_id)
    close_h = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_m = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    gtd = _eod_time_str(close_h, close_m - 1) if close_m > 0 else _eod_time_str(close_h - 1, 59)

    total_qty = int(shares)
    if total_qty <= 0:
        raise ValueError("shares must be > 0")

    frac = float(getattr(config, "PARTIAL_TP_FRACTION", 0.7))
    frac = min(max(frac, 0.0), 1.0)
    tp1_qty = int(round(total_qty * frac))
    # Ensure runner has at least 1 share when possible.
    if total_qty >= 2:
        tp1_qty = min(max(tp1_qty, 1), total_qty - 1)
    else:
        tp1_qty = 0
    runner_qty = total_qty - tp1_qty if tp1_qty > 0 else total_qty

    # ATR inputs are expressed as a percent (e.g. atr_pct=2.5 means 2.5%).
    atr_pct = float(atr_pct or 0.0)
    # Use existing config multipliers only to preserve the old TP1:TP2 ratio.
    partial_tp_pct = float(getattr(config, "PARTIAL_TP_PCT", 0.015) or 0.015)  # decimal fraction
    runner_cap_pct = float(getattr(config, "RUNNER_CAP_PCT", 0.065) or 0.065)  # decimal fraction
    ratio_tp2_to_tp1 = runner_cap_pct / partial_tp_pct if partial_tp_pct else 1.0

    if atr_pct <= 0:
        # Safety fallback: if ATR is missing, fall back to the old fixed-percent bracket
        # to avoid nonsensical 0-distance exits.
        tp1_frac = partial_tp_pct
        tp2_frac = runner_cap_pct
        sl_frac = float(getattr(config, "STOP_PCT", 0.025) or 0.025)
    else:
        # Spec: TP1 = 0.6 * ATR_pct, SL = 0.8 * ATR_pct (both in percent-units).
        tp1_pct_points = 0.6 * atr_pct
        sl_pct_points = 0.8 * atr_pct
        # Optional volatility-aware stop widening (percent-units).
        vol_sl_threshold = float(getattr(config, "VOLATILITY_SL_THRESHOLD", 5.0) or 5.0)
        vol_sl_buffer = float(getattr(config, "VOLATILITY_SL_BUFFER_PCT", 0.0) or 0.0)
        if atr_pct > vol_sl_threshold and vol_sl_buffer > 0:
            sl_pct_points += vol_sl_buffer

        # Convert percent-units to decimal fractions for price multipliers.
        tp1_frac = tp1_pct_points / 100.0
        sl_frac = sl_pct_points / 100.0
        tp2_frac = ratio_tp2_to_tp1 * tp1_frac

    if side == "LONG":
        tp1_price = round(fill_price * (1.0 + tp1_frac), 2)
        tp2_price = round(fill_price * (1.0 + tp2_frac), 2)
        stop_price = round(fill_price * (1.0 - sl_frac), 2)
        exit_action = "SELL"
    else:
        tp1_price = round(fill_price * (1.0 - tp1_frac), 2)
        tp2_price = round(fill_price * (1.0 - tp2_frac), 2)
        stop_price = round(fill_price * (1.0 + sl_frac), 2)
        exit_action = "BUY"

    tp1_trade = None
    if tp1_qty > 0:
        tp1 = Order()
        tp1.action = exit_action
        tp1.orderType = "LMT"
        tp1.totalQuantity = tp1_qty
        tp1.lmtPrice = tp1_price
        tp1.tif = "GTD"
        tp1.goodTillDate = gtd
        tp1.account = account_id
        tp1_trade = ib.placeOrder(contract, tp1)

    # Runner bracket (TP2 + SL) in OCA for runner_qty.
    tp2 = Order()
    tp2.action = exit_action
    tp2.orderType = "LMT"
    tp2.totalQuantity = runner_qty
    tp2.lmtPrice = tp2_price
    tp2.tif = "GTD"
    tp2.goodTillDate = gtd

    sl = Order()
    sl.action = exit_action
    sl.orderType = "STP"
    sl.totalQuantity = runner_qty
    sl.auxPrice = stop_price
    sl.tif = "GTD"
    sl.goodTillDate = gtd

    for o in (tp2, sl):
        o.account = account_id

    oca_group = f"EOD_RUNNER_{side}_{ticker}_{datetime.now(EASTERN).strftime('%H%M%S')}"
    ib.oneCancelsAll([tp2, sl], oca_group, 1)
    tp2_trade = ib.placeOrder(contract, tp2)
    sl_trade = ib.placeOrder(contract, sl)

    return {
        "tp1_price": float(tp1_price),
        "tp1_qty": float(tp1_qty),
        "tp1_trade": tp1_trade,
        "tp2_price": float(tp2_price),
        "runner_qty": float(runner_qty),
        "stop_price": float(stop_price),
        "tp2_trade": tp2_trade,
        "sl_trade": sl_trade,
    }


def cancel_gats_that_would_short(ib, account_id: str = "") -> None:
    """
    Kept for compatibility with older behavior.
    In this long/short runner we avoid GAT entry/exit orders, so this is usually a no-op.
    """
    account_id = resolve_account_id(ib, account_id)
    positions = dict(get_all_positions(ib, account_id))
    for trade in ib.openTrades():
        o = getattr(trade, "order", None)
        if o is None:
            continue
        if getattr(o, "orderType", "") != "MKT":
            continue
        if not getattr(o, "goodAfterTime", ""):
            continue
        contract = getattr(trade, "contract", None)
        if contract is None or getattr(contract, "secType", "") != "STK":
            continue
        symbol = (getattr(contract, "symbol", "") or "").upper().replace(".", "-")
        if not symbol:
            continue
        qty = int(getattr(o, "totalQuantity", 0) or 0)
        if qty <= 0:
            continue
        pos = float(positions.get(symbol, 0.0))
        # "Would short" is ambiguous in a long/short system; cancel only if it would increase abs exposure.
        if abs(pos) < qty:
            try:
                ib.cancelOrder(o)
            except Exception:
                pass

