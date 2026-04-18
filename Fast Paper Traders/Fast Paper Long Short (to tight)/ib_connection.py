# -----------------------------
# Fast Paper Long/Short – IBKR connection
# -----------------------------
import random
from ib_insync import IB, Stock
import config


def make_stock(symbol: str) -> Stock:
    symbol = symbol.replace(".", "-")
    return Stock(symbol, "SMART", "USD")


def connect_ib() -> IB:
    ib = IB()
    client_id = random.randint(1, 32)
    ib.connect(config.IB_HOST, config.IB_PORT, clientId=client_id)
    return ib


def get_account_value(ib: IB, account_id: str = "") -> float:
    """Return NetLiquidation in USD. Returns 0 if no USD value found."""
    account_id = account_id or ""
    if not account_id and ib.managedAccounts():
        account_id = ib.managedAccounts()[0]
    try:
        summary = ib.accountSummary(account_id)
    except Exception:
        summary = ib.accountValues(account_id)
    for tag in ("NetLiquidation", "TotalCashValue", "EquityWithLoanValue"):
        for av in summary:
            if av.tag != tag:
                continue
            try:
                val = float(av.value)
            except (TypeError, ValueError):
                continue
            if av.currency == "USD":
                return val
    return 0.0


def get_position_signed(ib: IB, account_id: str, ticker: str) -> float:
    """Return signed position size for ticker (positive=long, negative=short, 0=flat)."""
    account_id = account_id or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    try:
        positions = ib.positions(account_id)
    except Exception:
        return 0.0
    sym = ticker.upper().replace(".", "-")
    for pos in positions:
        p_sym = getattr(pos, "symbol", None) or getattr(getattr(pos, "contract", None), "symbol", "")
        if (p_sym or "").upper() == sym:
            p_pos = getattr(pos, "position", 0) or 0
            return float(p_pos)
    return 0.0


def get_position_size(ib: IB, account_id: str, ticker: str) -> float:
    """Backward compatible: return current long position size for ticker (0 if flat or short)."""
    return max(0.0, get_position_signed(ib, account_id, ticker))


def get_all_positions(ib: IB, account_id: str = "") -> list[tuple[str, float]]:
    """Return list of (symbol, position) for all stock positions. position is signed (negative = short)."""
    account_id = account_id or (ib.managedAccounts()[0] if ib.managedAccounts() else "")
    out = []
    try:
        positions = ib.positions(account_id)
    except Exception:
        return out
    for pos in positions:
        contract = getattr(pos, "contract", None)
        if contract is None:
            continue
        sec_type = getattr(contract, "secType", "") or ""
        if sec_type != "STK":
            continue
        p_sym = getattr(pos, "symbol", None) or getattr(contract, "symbol", "")
        if not p_sym:
            continue
        p_pos = getattr(pos, "position", 0) or 0
        out.append((str(p_sym).upper().replace(".", "-"), float(p_pos)))
    return out


def disconnect_ib(ib: IB) -> None:
    try:
        ib.disconnect()
    except Exception:
        pass

