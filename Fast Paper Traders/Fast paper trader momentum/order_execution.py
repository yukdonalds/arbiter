# -----------------------------
# Fast Paper Trader – Bracket orders (copy from mission)
# -----------------------------
from datetime import datetime
import pytz
from ib_insync import MarketOrder, Order
import config
from ib_connection import make_stock, get_position_size, get_all_positions

EASTERN = pytz.timezone("America/New_York")

def place_market_entry(ib, ticker: str, shares: float, account_id: str = ""):
    contract = make_stock(ticker)
    account_id = account_id or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    order = MarketOrder("BUY", shares)
    order.account = account_id
    order.tif = "DAY"
    trade = ib.placeOrder(contract, order)
    return trade

def place_market_sell(ib, ticker: str, shares: float, account_id: str = ""):
    """Sell only up to current long position; never short."""
    account_id = account_id or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    pos = get_position_size(ib, account_id, ticker)
    if pos <= 0:
        return
    sell_qty = min(shares, int(pos))
    if sell_qty <= 0:
        return
    contract = make_stock(ticker)
    order = MarketOrder("SELL", sell_qty)
    order.account = account_id
    order.tif = "DAY"
    ib.placeOrder(contract, order)

def _eod_time_str(hour: int, minute: int) -> str:
    """IB format: yyyymmdd hh:mm:ss {optional Timezone}"""
    now = datetime.now(EASTERN)
    return f"{now.strftime('%Y%m%d')} {hour:02d}:{minute:02d}:00 US/Eastern"

def place_stop_order(ib, ticker: str, shares: float, stop_price: float, account_id: str = ""):
    """Place a single GTD stop (SELL STP). Used when replacing stop for trailing/breakeven. Never sells more than long position."""
    account_id = account_id or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    pos = get_position_size(ib, account_id, ticker)
    if pos <= 0:
        return None
    sell_qty = min(shares, int(pos))
    if sell_qty <= 0:
        return None
    contract = make_stock(ticker)
    close_h = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_m = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)
    good_till = _eod_time_str(close_h, close_m - 1) if close_m > 0 else _eod_time_str(close_h - 1, 59)
    sl = Order()
    sl.action = "SELL"
    sl.orderType = "STP"
    sl.totalQuantity = sell_qty
    sl.auxPrice = round(stop_price, 2)
    sl.tif = "GTD"
    sl.goodTillDate = good_till
    sl.account = account_id
    trade = ib.placeOrder(contract, sl)
    return trade


def place_bracket_exits(ib, ticker: str, shares: float, fill_price: float, account_id: str = ""):
    contract = make_stock(ticker)
    account_id = account_id or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    take_profit_price = round(fill_price * (1 + config.TARGET_PCT), 2)
    stop_price = round(fill_price * (1 - config.STOP_PCT), 2)
    close_h = getattr(config, "CLOSE_POSITIONS_HOUR", 15)
    close_m = getattr(config, "CLOSE_POSITIONS_MINUTE", 45)

    # TP and SL in OCA (one fill cancels the other). GTD so they expire before market close.
    # No GAT: a good-after-time sell fires even when flat (e.g. after TP/SL) and causes a short.
    # We close at EOD via the script's market sell at 15:45. Ensure script runs until then.
    tp = Order()
    tp.action = "SELL"
    tp.orderType = "LMT"
    tp.totalQuantity = shares
    tp.lmtPrice = take_profit_price
    tp.tif = "GTD"
    tp.goodTillDate = _eod_time_str(close_h, close_m - 1) if close_m > 0 else _eod_time_str(close_h - 1, 59)
    sl = Order()
    sl.action = "SELL"
    sl.orderType = "STP"
    sl.totalQuantity = shares
    sl.auxPrice = stop_price
    sl.tif = "GTD"
    sl.goodTillDate = tp.goodTillDate
    for o in (tp, sl):
        o.account = account_id
    oca_group = f"EOD_{ticker}_{datetime.now(EASTERN).strftime('%H%M%S')}"
    ib.oneCancelsAll([tp, sl], oca_group, 1)
    ib.placeOrder(contract, tp)
    sl_trade = ib.placeOrder(contract, sl)
    return take_profit_price, stop_price, sl_trade


def cancel_all_gat_orders(ib) -> None:
    """Cancel every GAT (SELL MKT good-after-time) order so none can fire and cause a short."""
    for trade in ib.openTrades():
        o = getattr(trade, "order", None)
        if o is None:
            continue
        if getattr(o, "action", "") != "SELL":
            continue
        if getattr(o, "orderType", "") != "MKT":
            continue
        if not getattr(o, "goodAfterTime", ""):
            continue
        try:
            ib.cancelOrder(o)
        except Exception:
            pass


def cancel_gats_that_would_short(ib, account_id: str = "") -> None:
    """Cancel only GATs where current long position < order quantity (would cause short if GAT fires)."""
    account_id = account_id or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    positions = dict(get_all_positions(ib, account_id))
    for trade in ib.openTrades():
        o = getattr(trade, "order", None)
        if o is None:
            continue
        if getattr(o, "action", "") != "SELL":
            continue
        if getattr(o, "orderType", "") != "MKT":
            continue
        if not getattr(o, "goodAfterTime", ""):
            continue
        contract = getattr(trade, "contract", None)
        if contract is None:
            continue
        if getattr(contract, "secType", "") != "STK":
            continue
        symbol = (getattr(contract, "symbol", "") or "").upper().replace(".", "-")
        if not symbol:
            continue
        qty = int(getattr(o, "totalQuantity", 0) or 0)
        if qty <= 0:
            continue
        long_pos = max(0.0, positions.get(symbol, 0.0))
        if long_pos < qty:
            try:
                ib.cancelOrder(o)
            except Exception:
                pass
