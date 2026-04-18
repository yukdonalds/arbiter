# -----------------------------
# Fast Paper Trader – End-of-day report (daily + rolling stats, backtest-style)
# -----------------------------
"""
Generate a daily report when the session ends. Reads logs/trades.csv and
logs/daily_equity.csv for daily stats (today) and rolling/all-time stats.
Report saved to Reports/daily_report_YYYY-MM-DD.txt.
"""
import csv
import os
from datetime import datetime

import config


def _trade_log_path() -> str:
    return os.path.join(config.LOG_DIR, "trades.csv")


def _equity_log_path() -> str:
    return os.path.join(config.LOG_DIR, "daily_equity.csv")


def _load_trades() -> list[dict]:
    """Load all rows from trades.csv as list of dicts."""
    path = _trade_log_path()
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                row["PnL_Dollars"] = float(row.get("PnL_Dollars", 0))
                row["PnL_Pct"] = float(row.get("PnL_Pct", 0))
            except (ValueError, TypeError):
                continue
            out.append(row)
    return out


def _load_equity() -> list[dict]:
    """Load all rows from daily_equity.csv as list of dicts."""
    path = _equity_log_path()
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                row["Equity"] = float(row.get("Equity", 0))
                row["Drawdown"] = float(row.get("Drawdown", 0))
                row["Peak"] = float(row.get("Peak", 0))
            except (ValueError, TypeError):
                continue
            out.append(row)
    return out


def _daily_pnl_and_counts(trades: list[dict], date_str: str) -> tuple[float, int, int, int]:
    """Return (pnl_today, n_trades_today, wins_today, losses_today)."""
    day_trades = [t for t in trades if (t.get("Date") or "")[:10] == date_str[:10]]
    if not day_trades:
        return 0.0, 0, 0, 0
    pnl = sum(t["PnL_Dollars"] for t in day_trades)
    wins = sum(1 for t in day_trades if t["PnL_Dollars"] > 0)
    losses = sum(1 for t in day_trades if t["PnL_Dollars"] < 0)
    return pnl, len(day_trades), wins, losses


def _rolling_stats(trades: list[dict], equity_rows: list[dict], initial_capital: float) -> dict:
    """Compute rolling/all-time stats from full trade and equity history."""
    if not trades:
        return {
            "n_days": 0,
            "n_trades": 0,
            "total_pnl": 0.0,
            "ret_pct": 0.0,
            "avg_daily_pnl": 0.0,
            "best_date": "",
            "best_pnl": 0.0,
            "worst_date": "",
            "worst_pnl": 0.0,
            "max_dd": 0.0,
            "max_dd_pct": 0.0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "worst_5d_pnl": 0.0,
            "worst_5d_end_date": "",
            "max_stops_one_day": 0,
            "stops_dist_str": "0 stops: 0 days",
            "longest_losing_streak": 0,
            "worst_streak_pnl": 0.0,
            "worst_10_pnl": 0.0,
            "worst_10_pct": 0.0,
            "max_intraday_dd": 0.0,
            "max_single_day_intraday_dd": 0.0,
            "max_single_day_intraday_dd_date": "",
        }

    # Daily P&L series (one row per date with trades)
    from collections import defaultdict
    daily_pnl = defaultdict(float)
    for t in trades:
        d = (t.get("Date") or "")[:10]
        if d:
            daily_pnl[d] += t["PnL_Dollars"]

    dates_sorted = sorted(daily_pnl.keys())
    n_days = len(dates_sorted)
    n_trades = len(trades)
    total_pnl = sum(t["PnL_Dollars"] for t in trades)
    ret_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0.0
    avg_daily = (total_pnl / n_days) if n_days else 0.0

    best_date = max(daily_pnl.keys(), key=lambda d: daily_pnl[d]) if daily_pnl else ""
    best_pnl = daily_pnl.get(best_date, 0.0)
    worst_date = min(daily_pnl.keys(), key=lambda d: daily_pnl[d]) if daily_pnl else ""
    worst_pnl = daily_pnl.get(worst_date, 0.0)

    # Running capital and max drawdown (from equity curve if available, else from daily P&L)
    if equity_rows:
        # Use last equity per date as end-of-day capital
        by_date = defaultdict(list)
        for r in equity_rows:
            d = (r.get("Date") or "")[:10]
            if d:
                by_date[d].append(r)
        # For each date, take last Equity and Peak
        date_to_equity = {}
        date_to_peak = {}
        for d in sorted(by_date.keys()):
            rows = by_date[d]
            date_to_equity[d] = rows[-1]["Equity"]
            date_to_peak[d] = rows[-1]["Peak"]
        running = [initial_capital]
        for d in dates_sorted:
            running.append(date_to_equity.get(d, running[-1] + daily_pnl.get(d, 0)))
        rc = running[1:]
        peak = [initial_capital]
        for i, v in enumerate(rc):
            peak.append(max(peak[-1], v))
        peak = peak[1:]
        dd = [p - r for p, r in zip(peak, rc)]
        max_dd = max(dd) if dd else 0.0
        peak_at_trough = peak[dd.index(max_dd)] if dd and max_dd > 0 else 0.0
        max_dd_pct = (max_dd / peak_at_trough * 100) if peak_at_trough else 0.0
    else:
        running = [initial_capital]
        for d in dates_sorted:
            running.append(running[-1] + daily_pnl[d])
        rc = running[1:]
        peak = [initial_capital]
        for v in rc:
            peak.append(max(peak[-1], v))
        peak = peak[1:]
        dd = [p - r for p, r in zip(peak, rc)]
        max_dd = max(dd) if dd else 0.0
        peak_at_trough = peak[dd.index(max_dd)] if dd and max_dd > 0 else 0.0
        max_dd_pct = (max_dd / peak_at_trough * 100) if peak_at_trough else 0.0

    wins = sum(1 for t in trades if t["PnL_Dollars"] > 0)
    losses = sum(1 for t in trades if t["PnL_Dollars"] < 0)
    win_rate = (wins / n_trades * 100) if n_trades else 0.0

    # Worst 5-day rolling P&L (by calendar day)
    pnl_series = [daily_pnl[d] for d in dates_sorted]
    worst_5d_val = 0.0
    worst_5d_end_date = ""
    for i in range(len(pnl_series)):
        s = sum(pnl_series[max(0, i - 4) : i + 1])
        if s < worst_5d_val:
            worst_5d_val = s
            worst_5d_end_date = dates_sorted[i]

    # Stops: approximate as exit with PnL_Pct <= -2.5%
    STOP_LIKE_PCT = -2.5
    stops_per_day = defaultdict(int)
    for t in trades:
        d = (t.get("Date") or "")[:10]
        if d and t["PnL_Pct"] <= STOP_LIKE_PCT:
            stops_per_day[d] += 1
    max_stops_one_day = max(stops_per_day.values()) if stops_per_day else 0
    d0 = sum(1 for d in dates_sorted if stops_per_day.get(d, 0) == 0)
    d1 = sum(1 for d in dates_sorted if stops_per_day.get(d, 0) == 1)
    d2 = sum(1 for d in dates_sorted if stops_per_day.get(d, 0) == 2)
    d3p = sum(1 for d in dates_sorted if stops_per_day.get(d, 0) >= 3)
    stops_dist_str = f"0 stops: {d0} days, 1: {d1}, 2: {d2}, 3+: {d3p}"

    # Longest losing streak (consecutive losing trades)
    pl_series = [t["PnL_Dollars"] for t in trades]
    run_len, run_sum = 0, 0.0
    cur_len, cur_sum = 0, 0.0
    for i in range(len(pl_series)):
        if pl_series[i] < 0:
            cur_len += 1
            cur_sum += pl_series[i]
            if cur_len > run_len or (cur_len == run_len and cur_sum < run_sum):
                run_len, run_sum = cur_len, cur_sum
        else:
            cur_len, cur_sum = 0, 0.0

    # Worst 10 consecutive trades
    window = 10
    worst_10_pnl = 0.0
    worst_10_start_idx = 0
    if len(pl_series) >= window:
        for i in range(len(pl_series) - window + 1):
            s = sum(pl_series[i : i + window])
            if s < worst_10_pnl:
                worst_10_pnl = s
                worst_10_start_idx = i
    cap_before = initial_capital + (sum(pl_series[:worst_10_start_idx]) if worst_10_start_idx > 0 else 0)
    worst_10_pct = (worst_10_pnl / cap_before * 100) if cap_before > 0 else 0.0

    # Intraday drawdown from daily_equity (Drawdown column is running drawdown; per-day max is that day's intraday DD)
    max_intraday_dd = 0.0
    max_single_day_intraday_dd = 0.0
    max_single_day_intraday_dd_date = ""
    if equity_rows:
        by_date = defaultdict(list)
        for r in equity_rows:
            d = (r.get("Date") or "")[:10]
            if d:
                by_date[d].append(r["Drawdown"])
        for d, drawdowns in by_date.items():
            day_max_dd = max(drawdowns) if drawdowns else 0.0
            max_intraday_dd = max(max_intraday_dd, day_max_dd)
            if day_max_dd > max_single_day_intraday_dd:
                max_single_day_intraday_dd = day_max_dd
                max_single_day_intraday_dd_date = d

    return {
        "n_days": n_days,
        "n_trades": n_trades,
        "total_pnl": total_pnl,
        "ret_pct": ret_pct,
        "avg_daily_pnl": avg_daily,
        "best_date": best_date,
        "best_pnl": best_pnl,
        "worst_date": worst_date,
        "worst_pnl": worst_pnl,
        "max_dd": max_dd,
        "max_dd_pct": max_dd_pct,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "worst_5d_pnl": worst_5d_val,
        "worst_5d_end_date": worst_5d_end_date,
        "max_stops_one_day": max_stops_one_day,
        "stops_dist_str": stops_dist_str,
        "longest_losing_streak": run_len,
        "worst_streak_pnl": run_sum,
        "worst_10_pnl": worst_10_pnl,
        "worst_10_pct": worst_10_pct,
        "max_intraday_dd": max_intraday_dd,
        "max_single_day_intraday_dd": max_single_day_intraday_dd,
        "max_single_day_intraday_dd_date": max_single_day_intraday_dd_date,
        "final_capital": initial_capital + total_pnl,
    }


def generate_daily_report(
    report_date: str,
    start_capital: float,
    final_capital: float,
    peak_capital: float,
    ib_data: dict | None = None,
    daily_stats_override: dict | None = None,
    trades_override: list | None = None,
) -> str | None:
    """
    Write a daily report to Reports/daily_report_YYYY-MM-DD.txt.
    report_date: YYYY-MM-DD
    ib_data: optional dict from IB API (for supplement section)
    daily_stats_override: optional {n_trades, pnl, wins, losses} from IB executions
    trades_override: optional merged trades (logs + IB) for rolling stats
    Returns path to report file, or None if Reports dir not writable.
    """
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    report_path = os.path.join(config.REPORT_DIR, f"daily_report_{report_date[:10]}.txt")

    trades = trades_override if trades_override is not None else _load_trades()
    equity_rows = _load_equity()

    # Daily stats: use override from IB when available, else from logs
    if daily_stats_override:
        n_trades_today = int(daily_stats_override.get("n_trades", 0))
        daily_pnl = float(daily_stats_override.get("pnl", 0))
        wins_today = int(daily_stats_override.get("wins", 0))
        losses_today = int(daily_stats_override.get("losses", 0))
    else:
        daily_pnl, n_trades_today, wins_today, losses_today = _daily_pnl_and_counts(trades, report_date[:10])

    # Day-aggregate capital from logs: first and last equity snapshot for this date (so one report = total for the day)
    date_only = report_date[:10]
    today_equity = [r for r in equity_rows if (r.get("Date") or "")[:10] == date_only]
    if today_equity:
        day_start_capital = float(today_equity[0].get("Equity", 0))
        day_end_capital = float(today_equity[-1].get("Equity", 0))
        day_peak_capital = max(float(r.get("Peak", 0)) for r in today_equity)
        n_sessions_today = len(today_equity)  # one equity row per session end
    else:
        day_start_capital = start_capital
        day_end_capital = final_capital
        day_peak_capital = peak_capital
        n_sessions_today = 1

    # Initial capital for rolling: use first equity row (any date) if available, else start_capital
    initial_capital = start_capital
    if equity_rows:
        first_equity = float(equity_rows[0].get("Equity", 0))
        if first_equity > 0 and len(equity_rows) > 1:
            initial_capital = first_equity
    roll = _rolling_stats(trades, equity_rows, initial_capital)

    # Theoretical one-day loss (same as backtest)
    theoretical_one_day_pct = (
        getattr(config, "MAX_POSITIONS", 10)
        * getattr(config, "MAX_POSITION_PCT", 0.15)
        * getattr(config, "STOP_PCT", 0.03)
        * 100
    )

    lines = [
        "========================================================================\n  Fast Paper Trader – Daily Report  " + report_date[:10] + "\n========================================================================\n",
        "\n  Generated: " + datetime.now().strftime("%Y-%m-%d %H:%M") + "\n",
        "  (One report per day; includes all sessions run on this date. File overwritten on each shutdown.)\n",
        "------------------------------------------------------------------------\n  PARAMETERS\n------------------------------------------------------------------------\n",
        f"  First session end (today)     ${day_start_capital:,.2f}",
        f"  Last session end (today)     ${day_end_capital:,.2f}",
        f"  Sessions today               {n_sessions_today}",
        "",
        "  Capital / position rules",
        f"    Max positions per day         {getattr(config, 'MAX_POSITIONS', 10)}",
        f"    Max position per trade        {getattr(config, 'MAX_POSITION_PCT', 0.15) * 100:.0f}% of capital",
        f"    Entry order timeout           {getattr(config, 'ENTRY_ORDER_TIMEOUT_SECONDS', 900) // 60} min",
        "",
        "  Exit rules",
        f"    Target                       {getattr(config, 'TARGET_PCT', 0.05) * 100:.0f}%",
        f"    Stop                         {getattr(config, 'STOP_PCT', 0.03) * 100:.0f}%",
        "",
        "------------------------------------------------------------------------\n  DAILY STATS (today" + (" – from IB API" if daily_stats_override else " – all sessions") + ")\n------------------------------------------------------------------------\n",
        f"  Trades today                  {n_trades_today}",
        f"  P&L today                     ${daily_pnl:,.2f}",
        f"  Wins / Losses today           {wins_today} / {losses_today}",
        f"  Capital at first session end  ${day_start_capital:,.2f}",
        f"  Capital at last session end   ${day_end_capital:,.2f}",
        f"  Peak capital (today)          ${day_peak_capital:,.2f}",
        "",
        "------------------------------------------------------------------------\n  ROLLING / ALL-TIME STATS\n------------------------------------------------------------------------\n",
        f"  Trading days with trades      {roll['n_days']}",
        f"  Total trades                  {roll['n_trades']}",
        f"  Initial capital (reference)   ${initial_capital:,.2f}",
        f"  Final capital (from logs)     ${roll.get('final_capital', initial_capital + roll['total_pnl']):,.2f}",
        f"  Total P&L                    ${roll['total_pnl']:,.2f}",
        f"  Return                       {roll['ret_pct']:.2f}%",
        f"  Avg daily P&L                 ${roll['avg_daily_pnl']:,.2f}",
        f"  Best day                     {roll['best_date']}  P&L ${roll['best_pnl']:,.2f}",
        f"  Worst day                    {roll['worst_date']}  P&L ${roll['worst_pnl']:,.2f}",
        f"  Max drawdown                 ${roll['max_dd']:,.2f} ({roll['max_dd_pct']:.1f}%)",
        f"  Win rate                     {roll['win_rate']:.1f}% ({roll['wins']} wins / {roll['losses']} losses)",
        "",
        "------------------------------------------------------------------------\n  CLUSTERING & RISK (quick wins)\n------------------------------------------------------------------------\n",
        f"  Worst 5-day rolling P&L      ${roll['worst_5d_pnl']:,.2f}  (ending {roll['worst_5d_end_date']})",
        f"  Max stops in one day         {roll['max_stops_one_day']} (approx: PnL <= -2.5%)",
        f"  Stops per day distribution   {roll['stops_dist_str']}",
        f"  Longest losing streak        {roll['longest_losing_streak']} trades  P&L ${roll['worst_streak_pnl']:,.2f}",
        f"  Worst 10 consecutive trades  P&L ${roll['worst_10_pnl']:,.2f}  ({roll['worst_10_pct']:.1f}% of capital at start of run)",
        f"  Theoretical one-day loss     {theoretical_one_day_pct:.1f}% of capital (all positions stop, before slippage)",
        "",
        "------------------------------------------------------------------------\n  INTRADAY DRAWDOWN\n------------------------------------------------------------------------\n",
        f"  Max intraday drawdown        ${roll['max_intraday_dd']:,.2f}",
        f"  Max single-day intraday DD  ${roll['max_single_day_intraday_dd']:,.2f}  on {roll['max_single_day_intraday_dd_date']}\n",
    ]

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return report_path
    except OSError:
        return None
