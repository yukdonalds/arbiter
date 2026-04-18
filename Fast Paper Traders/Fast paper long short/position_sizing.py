# -----------------------------
# Fast Paper Long/Short – Position sizing
# -----------------------------
import config


def size_per_trade(n_signals_today: int, capital: float, entry_price: float) -> tuple[float, float]:
    if n_signals_today < 1 or capital <= 0 or entry_price <= 0:
        return 0.0, 0.0
    n = min(n_signals_today, config.MAX_POSITIONS)
    size_pct = min(config.MAX_POSITION_PCT, 1.0 / n)
    dollar = size_pct * capital
    shares = dollar / entry_price
    return dollar, shares

