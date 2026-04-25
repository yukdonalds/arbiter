# -----------------------------
# Fast Paper Long/Short – IBKR connection
# -----------------------------
import time
from ib_insync import IB, Stock
import config


def make_stock(symbol: str) -> Stock:
    symbol = symbol.replace(".", "-")
    return Stock(symbol, "SMART", "USD")


def _managed_accounts_clean(ib: IB) -> list[str]:
    accounts = []
    for acc in ib.managedAccounts() or []:
        s = str(acc or "").strip()
        if s:
            accounts.append(s)
    return accounts


def resolve_account_id(ib: IB, account_id: str = "") -> str:
    explicit = str(account_id or "").strip()
    if explicit:
        return explicit
    cfg_account = str(getattr(config, "IB_ACCOUNT", "") or "").strip()
    if cfg_account:
        return cfg_account
    accounts = _managed_accounts_clean(ib)
    return accounts[0] if accounts else ""


def connect_ib() -> IB:
    ib = IB()
    # Avoid sub-account auto-sync requests; some TWS sessions return blank groups.
    ib.MaxSyncedSubAccounts = 0
    client_id = int(getattr(config, "IB_CLIENT_ID", 1) or 1)
    connect_timeout = float(getattr(config, "IB_CONNECT_TIMEOUT_SEC", 20.0) or 20.0)
    preferred_account = str(getattr(config, "IB_ACCOUNT", "") or "").strip()
    ib.connect(
        config.IB_HOST,
        config.IB_PORT,
        clientId=client_id,
        timeout=connect_timeout,
        account=preferred_account or "",
    )
    return ib


def get_account_value(ib: IB, account_id: str = "") -> float:
    """
    Return NetLiquidation in USD using non-blocking cached account values.
    Returns 0 if no USD value is available yet.
    """
    account_id = resolve_account_id(ib, account_id)
    if not account_id:
        return 0.0
    summary = ib.accountValues(account_id)
    if not summary:
        warmup_sec = float(getattr(config, "IB_ACCOUNT_VALUES_WARMUP_SEC", 4.0) or 4.0)
        poll_sec = float(getattr(config, "IB_ACCOUNT_VALUES_POLL_SEC", 0.2) or 0.2)
        deadline = time.time() + max(0.5, warmup_sec)
        while time.time() < deadline:
            ib.sleep(max(0.05, poll_sec))
            summary = ib.accountValues(account_id)
            if summary:
                break
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
    account_id = resolve_account_id(ib, account_id)
    if not account_id:
        return 0.0
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
    account_id = resolve_account_id(ib, account_id)
    if not account_id:
        return []
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

