# -----------------------------
# Fast Paper Trader – Trade and equity logging (copy from mission)
# -----------------------------
import os
import csv
import config

def _trade_log_path() -> str:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    return os.path.join(config.LOG_DIR, "trades.csv")

def _equity_log_path() -> str:
    return os.path.join(config.LOG_DIR, "daily_equity.csv")

def init_trade_log():
    p = _trade_log_path()
    if os.path.exists(p):
        return
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "Date", "Ticker", "EntryTime", "EntryPrice", "Shares", "Target", "Stop",
            "ExitTime", "ExitPrice", "PnL_Dollars", "PnL_Pct"
        ])

def log_trade(
    date: str,
    ticker: str,
    entry_time: str,
    entry_price: float,
    shares: float,
    target_price: float,
    stop_price: float,
    exit_time: str,
    exit_price: float,
    pnl_dollars: float,
    pnl_pct: float,
):
    with open(_trade_log_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            date, ticker, entry_time, entry_price, shares, target_price, stop_price,
            exit_time, exit_price, round(pnl_dollars, 2), round(pnl_pct, 4)
        ])

def init_equity_log():
    p = _equity_log_path()
    if os.path.exists(p):
        return
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Equity", "Drawdown", "Peak"])

def log_daily_equity(date: str, equity: float, drawdown: float, peak: float):
    with open(_equity_log_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([date, round(equity, 2), round(drawdown, 2), round(peak, 2)])
