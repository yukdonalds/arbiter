# -----------------------------
# Fast Paper Long/Short – Stats collection for analysis
# -----------------------------
"""
Log signal quality, trade outcomes (MFE/MAE), execution timing, and daily regime
for later analysis. Files: logs/signals.csv, logs/trade_outcomes.csv, logs/daily_regime.csv
"""

import csv
import os
import config


def _stats_dir() -> str:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    return config.LOG_DIR


def _signals_path() -> str:
    return os.path.join(_stats_dir(), "signals.csv")


def _trade_outcomes_path() -> str:
    return os.path.join(_stats_dir(), "trade_outcomes.csv")


def _daily_regime_path() -> str:
    return os.path.join(_stats_dir(), "daily_regime.csv")


def init_signal_log():
    p = _signals_path()
    if os.path.exists(p):
        return
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "Date",
                "Time",
                "Ticker",
                "Side",
                "BiasDir",
                "BiasStrength",
                "pct_change_1d",
                "rel_vol",
                "atr_pct",
                "dist_vwap_pct",
                "score",
                "rank_position",
                "signal_price",
                "filled",
                "fill_price",
                "fill_time",
                "slippage_pct",
                "time_to_fill_sec",
            ]
        )


def log_signal(
    date: str,
    time_str: str,
    ticker: str,
    side: str,
    bias_dir: str,
    bias_strength: float,
    pct_change_1d: float,
    rel_vol: float,
    atr_pct: float,
    dist_vwap_pct: float,
    score: float,
    rank_position: int,
    signal_price: float,
    filled: bool = False,
    fill_price: float = 0,
    fill_time: str = "",
    slippage_pct: float = 0,
    time_to_fill_sec: float = 0,
):
    with open(_signals_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                date,
                time_str,
                ticker,
                (side or "").upper(),
                (bias_dir or "").upper(),
                round(float(bias_strength or 0), 4),
                round(pct_change_1d, 4),
                round(rel_vol, 4),
                round(atr_pct, 4),
                round(dist_vwap_pct, 4),
                round(score, 4),
                rank_position,
                round(signal_price, 2),
                "Y" if filled else "N",
                round(fill_price, 2) if fill_price else "",
                fill_time,
                round(slippage_pct, 4) if slippage_pct else "",
                round(time_to_fill_sec, 1) if time_to_fill_sec else "",
            ]
        )


def init_trade_outcomes_log():
    p = _trade_outcomes_path()
    if os.path.exists(p):
        return
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "Date",
                "Ticker",
                "Side",
                "EntryTime",
                "ExitTime",
                "EntryPrice",
                "ExitPrice",
                "SignalPrice",
                "Shares",
                "Target",
                "Stop",
                "PnL_Dollars",
                "PnL_Pct",
                "ExitReason",
                "MFE_pct",
                "MAE_pct",
                "hit_3_before_3",
                "slippage_entry_pct",
                "slippage_exit_pct",
            ]
        )


def log_trade_outcome(
    date: str,
    ticker: str,
    side: str,
    entry_time: str,
    exit_time: str,
    entry_price: float,
    exit_price: float,
    signal_price: float,
    shares: float,
    target_price: float,
    stop_price: float,
    pnl_dollars: float,
    pnl_pct: float,
    exit_reason: str,
    mfe_pct: float = 0,
    mae_pct: float = 0,
    hit_3_before_3: bool = False,
    slippage_entry_pct: float = 0,
    slippage_exit_pct: float = 0,
):
    with open(_trade_outcomes_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                date,
                ticker,
                (side or "").upper(),
                entry_time,
                exit_time,
                round(entry_price, 2),
                round(exit_price, 2),
                round(signal_price, 2),
                round(shares, 2),
                round(target_price, 2),
                round(stop_price, 2),
                round(pnl_dollars, 2),
                round(pnl_pct, 4),
                exit_reason,
                round(mfe_pct, 4) if mfe_pct else "",
                round(mae_pct, 4) if mae_pct else "",
                "Y" if hit_3_before_3 else "N",
                round(slippage_entry_pct, 4) if slippage_entry_pct else "",
                round(slippage_exit_pct, 4) if slippage_exit_pct else "",
            ]
        )


def init_daily_regime_log():
    p = _daily_regime_path()
    if os.path.exists(p):
        return
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "Date",
                "signals_generated",
                "trades_placed",
                "trades_filled",
                "trades_exited",
                "total_pnl",
                "win_count",
                "loss_count",
            ]
        )


def log_daily_regime(
    date: str,
    signals_generated: int,
    trades_placed: int,
    trades_filled: int,
    trades_exited: int,
    total_pnl: float,
    win_count: int,
    loss_count: int,
):
    with open(_daily_regime_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                date,
                signals_generated,
                trades_placed,
                trades_filled,
                trades_exited,
                round(total_pnl, 2),
                win_count,
                loss_count,
            ]
        )

