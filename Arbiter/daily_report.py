# -----------------------------
# Fast Paper Long/Short – End-of-day report
# -----------------------------
"""
Generate a daily report when the session ends. Reads logs/trades.csv and
logs/daily_equity.csv for daily stats (today) and rolling/all-time stats.
Report saved to Reports/daily_report_YYYY-MM-DD.txt and optionally mirrored
to config.REPORT_MIRROR_DIR when set.

Daily account P&L headline uses broker Net Liquidation change by default
(config REPORT_DAILY_PNL_PRIMARY = "broker") so headline numbers align with IB;
trade rows remain the strategy model for execution review.
"""

import csv
import os
import textwrap
from collections import defaultdict
from datetime import datetime

import config

# Banner width; all body lines are wrapped/clamped to this width.
REPORT_LINE_WIDTH = 85


def _report_ruler(char: str) -> str:
    return char * REPORT_LINE_WIDTH


def _report_section_heading(title: str) -> list[str]:
    return [_report_ruler("-"), f"  {title}", _report_ruler("-"), ""]


def _wrap_report_prose(paragraph: str) -> list[str]:
    """Reflow prose with a 2-space left margin; each output line is at most REPORT_LINE_WIDTH."""
    p = " ".join(paragraph.split())
    if not p:
        return [""]
    tw = textwrap.TextWrapper(
        width=REPORT_LINE_WIDTH,
        initial_indent="  ",
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=True,
    )
    try:
        return tw.wrap(p)
    except ValueError:
        tw.break_long_words = True
        return tw.wrap(p)


def _clamp_line(line: str) -> list[str]:
    """One logical line -> one or more lines, each length <= REPORT_LINE_WIDTH."""
    line = line.rstrip("\r\n")
    if line == "":
        return [""]
    if line.strip("=") == "" and line:
        return [_report_ruler("=")]
    if line.strip("-") == "" and line:
        return [_report_ruler("-")]
    if len(line) <= REPORT_LINE_WIDTH:
        return [line]
    lead = len(line) - len(line.lstrip(" "))
    base_indent = line[:lead]
    body = line[lead:]
    subsequent = base_indent + ("  " if lead <= 2 else " ")
    if len(subsequent) >= REPORT_LINE_WIDTH - 12:
        subsequent = "    "
    tw = textwrap.TextWrapper(
        width=REPORT_LINE_WIDTH,
        initial_indent=base_indent,
        subsequent_indent=subsequent,
        break_long_words=False,
        break_on_hyphens=True,
    )
    try:
        return tw.wrap(body)
    except ValueError:
        tw.break_long_words = True
        return tw.wrap(body)


def _finalize_report_lines(lines: list[str]) -> list[str]:
    """Flatten embedded newlines and enforce REPORT_LINE_WIDTH on every line."""
    out: list[str] = []
    for row in lines:
        for piece in row.split("\n"):
            out.extend(_clamp_line(piece))
    return out


def _resolved_report_mirror_dir() -> str:
    raw = (getattr(config, "REPORT_MIRROR_DIR", "") or "").strip()
    if not raw:
        return ""
    return os.path.normpath(os.path.expandvars(os.path.expanduser(raw)))


def _log_report_mirror(msg: str) -> None:
    path = getattr(config, "SESSION_LOG_FILE", None)
    if not path:
        return
    try:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [REPORT_MIRROR] {msg}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _mirror_daily_report_file(report_path: str) -> None:
    """Copy report to REPORT_MIRROR_DIR; plain read/write (OneDrive-friendly)."""
    mirror_dir = _resolved_report_mirror_dir()
    if not mirror_dir:
        return
    mirror_path = os.path.join(mirror_dir, os.path.basename(report_path))
    try:
        os.makedirs(mirror_dir, exist_ok=True)
        with open(report_path, "rb") as f:
            data = f.read()
        with open(mirror_path, "wb") as f:
            f.write(data)
        _log_report_mirror(f"OK -> {mirror_path}")
    except OSError as e:
        _log_report_mirror(f"FAILED: {e} | dir={mirror_dir!r} | file={mirror_path!r}")


def _trade_log_path() -> str:
    return os.path.join(config.LOG_DIR, "trades.csv")


def _equity_log_path() -> str:
    return os.path.join(config.LOG_DIR, "daily_equity.csv")


def _daily_regime_path() -> str:
    return os.path.join(config.LOG_DIR, "daily_regime.csv")


def _load_regime_row_for_date(date_only: str) -> dict | None:
    path = _daily_regime_path()
    if not os.path.isfile(path):
        return None
    last: dict | None = None
    with open(path, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if (row.get("Date") or "")[:10] == date_only[:10]:
                last = row
    return last


def _equity_series_has_funding_jump(equity_rows: list[dict]) -> bool:
    """True if consecutive broker snapshots look like funding/reset, not P&L drift."""
    if len(equity_rows) < 2:
        return False
    # Sort by date string then order in file (already append order)
    for i in range(1, len(equity_rows)):
        prev = float(equity_rows[i - 1].get("Equity", 0) or 0)
        curr = float(equity_rows[i].get("Equity", 0) or 0)
        if prev <= 0:
            continue
        jump = abs(curr - prev)
        if jump > 2000 and jump / prev > 0.15:
            return True
    return False


def _equity_last_close_by_date(equity_rows: list[dict]) -> dict[str, float]:
    """Calendar date -> session-close Equity from daily_equity.csv (last row per date)."""
    by_date: dict[str, list] = defaultdict(list)
    for r in equity_rows:
        d = (r.get("Date") or "")[:10]
        if d:
            by_date[d].append(r)
    return {d: float(rows[-1].get("Equity", 0)) for d, rows in by_date.items()}


def _avg_broker_daily_delta_to_date(equity_rows: list[dict], date_cap: str) -> float:
    """Mean day-over-day Net Liq change for dates <= date_cap (ordered dates in log only)."""
    close_map = _equity_last_close_by_date(equity_rows)
    ds = sorted(d for d in close_map if d <= date_cap[:10])
    if len(ds) < 2:
        return 0.0
    deltas = []
    raw = []
    for i in range(1, len(ds)):
        prev_e = close_map[ds[i - 1]]
        delta = close_map[ds[i]] - prev_e
        raw.append(delta)
        # Omit funding / reset steps so they don't dominate the average.
        if prev_e > 0 and abs(delta) > 2000 and abs(delta) / prev_e > 0.15:
            continue
        deltas.append(delta)
    if deltas:
        return sum(deltas) / len(deltas)
    if raw:
        return sum(raw) / len(raw)
    return 0.0


def _scan_trade_rows_for_issues(day_trades: list[dict]) -> list[str]:
    """Human-readable flags for obviously bad bookkeeping rows (e.g. $0 exit price)."""
    out: list[str] = []
    for t in day_trades:
        sym = t.get("Ticker", "?")
        x = float(t.get("ExitPrice", 0) or 0)
        if x <= 0 and (t.get("EntryPrice") or 0) and float(t.get("Shares", 0) or 0) > 0:
            out.append(f"{sym}: exit price {x:.2f} (booked P&L likely wrong)")
    return out


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
                row["ExitPrice"] = float(row.get("ExitPrice", 0) or 0)
                row["EntryPrice"] = float(row.get("EntryPrice", 0) or 0)
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


def _trade_outcomes_path() -> str:
    return os.path.join(config.LOG_DIR, "trade_outcomes.csv")


def _load_trade_outcomes() -> list[dict]:
    """Load all rows from trade_outcomes.csv as list of dicts."""
    path = _trade_outcomes_path()
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
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
            "broker_equity_first": 0.0,
            "broker_equity_last": 0.0,
            "broker_date_first": "",
            "broker_date_last": "",
            "broker_curve_pct": 0.0,
            "equity_series_has_jump": False,
            "trade_implied_final_equity": 0.0,
        }

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

    if equity_rows:
        by_date = defaultdict(list)
        for r in equity_rows:
            d = (r.get("Date") or "")[:10]
            if d:
                by_date[d].append(r)
        date_to_equity = {}
        for d in sorted(by_date.keys()):
            rows = by_date[d]
            date_to_equity[d] = rows[-1]["Equity"]
        running = [initial_capital]
        for d in dates_sorted:
            running.append(date_to_equity.get(d, running[-1] + daily_pnl.get(d, 0)))
        rc = running[1:]
        peak = [initial_capital]
        for v in rc:
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
    decisive_trades = wins + losses
    win_rate = (wins / decisive_trades * 100) if decisive_trades else 0.0

    pnl_series = [daily_pnl[d] for d in dates_sorted]
    worst_5d_val = 0.0
    worst_5d_end_date = ""
    for i in range(len(pnl_series)):
        s = sum(pnl_series[max(0, i - 4) : i + 1])
        if s < worst_5d_val:
            worst_5d_val = s
            worst_5d_end_date = dates_sorted[i]

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

    pl_series = [t["PnL_Dollars"] for t in trades]
    run_len, run_sum = 0, 0.0
    cur_len, cur_sum = 0, 0.0
    for v in pl_series:
        if v < 0:
            cur_len += 1
            cur_sum += v
            if cur_len > run_len or (cur_len == run_len and cur_sum < run_sum):
                run_len, run_sum = cur_len, cur_sum
        else:
            cur_len, cur_sum = 0, 0.0

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

    broker_equity_first = float(equity_rows[0].get("Equity", 0)) if equity_rows else initial_capital
    broker_equity_last = float(equity_rows[-1].get("Equity", 0)) if equity_rows else initial_capital + total_pnl
    broker_date_first = (equity_rows[0].get("Date") or "")[:10] if equity_rows else ""
    broker_date_last = (equity_rows[-1].get("Date") or "")[:10] if equity_rows else ""
    equity_series_has_jump = _equity_series_has_funding_jump(equity_rows) if equity_rows else False
    trade_implied_final_equity = initial_capital + total_pnl
    if equity_rows and broker_equity_first > 0:
        broker_curve_pct = (broker_equity_last - broker_equity_first) / broker_equity_first * 100
    elif (not equity_rows) and initial_capital > 0:
        broker_curve_pct = (trade_implied_final_equity - initial_capital) / initial_capital * 100
    else:
        broker_curve_pct = 0.0

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
        "broker_equity_first": broker_equity_first,
        "broker_equity_last": broker_equity_last,
        "broker_date_first": broker_date_first,
        "broker_date_last": broker_date_last,
        "broker_curve_pct": broker_curve_pct,
        "equity_series_has_jump": equity_series_has_jump,
        "trade_implied_final_equity": trade_implied_final_equity,
        "final_capital": broker_equity_last if equity_rows else trade_implied_final_equity,
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
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    report_path = os.path.join(config.REPORT_DIR, f"daily_report_{report_date[:10]}.txt")

    trades = trades_override if trades_override is not None else _load_trades()
    equity_rows = _load_equity()
    trade_outcomes = _load_trade_outcomes()

    if daily_stats_override:
        n_trades_today = int(daily_stats_override.get("n_trades", 0))
        daily_pnl = float(daily_stats_override.get("pnl", 0))
        wins_today = int(daily_stats_override.get("wins", 0))
        losses_today = int(daily_stats_override.get("losses", 0))
    else:
        daily_pnl, n_trades_today, wins_today, losses_today = _daily_pnl_and_counts(trades, report_date[:10])

    date_only = report_date[:10]
    trades_to_date = [t for t in trades if (t.get("Date") or "")[:10] <= date_only]
    equity_to_date = [r for r in equity_rows if (r.get("Date") or "")[:10] <= date_only]
    outcomes_to_date = [r for r in trade_outcomes if (r.get("Date") or "")[:10] <= date_only]
    today_equity = [r for r in equity_rows if (r.get("Date") or "")[:10] == date_only]
    prior_equity = [r for r in equity_rows if (r.get("Date") or "")[:10] < date_only]
    prior_close_capital = float(prior_equity[-1].get("Equity", 0)) if prior_equity else 0.0
    if today_equity:
        day_close_capital = float(today_equity[-1].get("Equity", 0))
        if prior_close_capital > 0:
            day_open_capital = prior_close_capital
        else:
            day_open_capital = float(today_equity[0].get("Equity", 0))
        day_peak_capital = max(float(r.get("Peak", 0)) for r in today_equity)
        n_sessions_today = len(today_equity)
    else:
        day_open_capital = prior_close_capital if prior_close_capital > 0 else start_capital
        day_close_capital = day_open_capital
        day_peak_capital = peak_capital if peak_capital > 0 else day_open_capital
        n_sessions_today = 0

    initial_capital = start_capital
    if equity_to_date:
        first_equity = float(equity_to_date[0].get("Equity", 0))
        if first_equity > 0:
            initial_capital = first_equity
    roll = _rolling_stats(trades_to_date, equity_to_date, initial_capital)
    avg_trades_per_day = (roll["n_trades"] / roll["n_days"]) if roll["n_days"] else 0.0
    decisive_today = wins_today + losses_today
    win_rate_today = (wins_today / decisive_today * 100.0) if decisive_today else 0.0
    day_return_pct = (daily_pnl / day_open_capital * 100.0) if day_open_capital else 0.0
    capital_delta_today = day_close_capital - day_open_capital
    broker_return_pct = (capital_delta_today / day_open_capital * 100.0) if day_open_capital else 0.0
    pnl_recon_gap = capital_delta_today - daily_pnl
    # Identity: capital_delta_today == daily_pnl + pnl_recon_gap (always, by definition)
    best_day = roll.get("best_date") or "N/A"
    worst_day = roll.get("worst_date") or "N/A"

    # Exit reason mix for today's exits from trade_outcomes.csv.
    today_outcomes = [r for r in outcomes_to_date if (r.get("Date") or "")[:10] == date_only]
    exit_counts: dict[str, int] = {}
    for row in today_outcomes:
        reason = str(row.get("ExitReason") or "").upper() or "UNKNOWN"
        exit_counts[reason] = exit_counts.get(reason, 0) + 1
    exit_mix_today = ", ".join(f"{k}:{v}" for k, v in sorted(exit_counts.items())) if exit_counts else "N/A"

    day_trades_list = [t for t in trades if (t.get("Date") or "")[:10] == date_only]
    trade_row_issues = _scan_trade_rows_for_issues(day_trades_list)
    regime_row = _load_regime_row_for_date(date_only)
    regime_exited = None
    regime_filled = None
    if regime_row:
        try:
            regime_exited = int(float(regime_row.get("trades_exited") or 0))
            regime_filled = int(float(regime_row.get("trades_filled") or 0))
        except (ValueError, TypeError):
            pass

    primary_src_label = str(getattr(config, "REPORT_DAILY_PNL_PRIMARY", "broker")).strip().lower()
    use_broker_primary = primary_src_label != "trade_log"
    primary_dollars = capital_delta_today if use_broker_primary else daily_pnl
    primary_pct = broker_return_pct if use_broker_primary else day_return_pct
    avg_broker_daily_delta = _avg_broker_daily_delta_to_date(equity_to_date, date_only)

    theoretical_one_day_pct = (
        getattr(config, "MAX_POSITIONS", 10)
        * getattr(config, "MAX_POSITION_PCT", 0.15)
        * getattr(config, "STOP_PCT", 0.03)
        * 100
    )

    roll_broker_ret_str = (
        "N/A (equity log has funding/resets; do not use first-to-last pct as strategy return)"
        if roll.get("equity_series_has_jump")
        else f"{roll['broker_curve_pct']:.2f}%"
    )
    roll_trade_implied = roll.get("trade_implied_final_equity", initial_capital + roll.get("total_pnl", 0))

    title_line = f"  Fast Paper Long/Short – Daily Report  {report_date[:10]}"
    if len(title_line) > REPORT_LINE_WIDTH:
        title_line = title_line[: REPORT_LINE_WIDTH - 1].rstrip() + "…"

    lines = [
        _report_ruler("="),
        title_line,
        _report_ruler("="),
        "",
        f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        *_report_section_heading("RULES"),
        "  Capital / position limits",
        f"    Max positions per day         {getattr(config, 'MAX_POSITIONS', 10)}",
        f"    Max position per trade        {getattr(config, 'MAX_POSITION_PCT', 0.15) * 100:.0f}% of capital",
        f"    Entry order timeout           {getattr(config, 'ENTRY_ORDER_TIMEOUT_SECONDS', 900) // 60} min",
        "",
        "  Exit rules",
        f"    TP1                          {getattr(config, 'PARTIAL_TP_PCT', getattr(config, 'TARGET_PCT', 0.05)) * 100:.1f}%"
        f"  x{getattr(config, 'PARTIAL_TP_FRACTION', 0.0) * 100:.0f}%",
        f"    Runner cap                   {getattr(config, 'RUNNER_CAP_PCT', getattr(config, 'TARGET_PCT', 0.05)) * 100:.0f}%",
        f"    Stop                         {getattr(config, 'STOP_PCT', 0.03) * 100:.0f}%",
        f"    Report daily P&L primary     {getattr(config, 'REPORT_DAILY_PNL_PRIMARY', 'broker')} "
        f"(broker = IB Net Liq vs prior close)",
        "",
        *_report_section_heading("PRIMARY — DAILY ACCOUNT P&L (what you care about vs IB)"),
        (
            f"  Daily account P&L              ${primary_dollars:,.2f}  ({primary_pct:+.2f}% vs prior close)"
            if use_broker_primary
            else f"  Daily model P&L (trade rows)   ${primary_dollars:,.2f}  ({primary_pct:+.2f}% vs prior close)"
        ),
        (
            "    Source: broker Net Liquidation change from daily_equity.csv (aligned with IB statement)."
            if use_broker_primary
            else "    Source: sum of trades.csv rows for this date (strategy bookkeeping)."
        ),
        (
            f"  Strategy model (trade rows)    ${daily_pnl:,.2f}  ({day_return_pct:+.2f}% vs prior close)"
            if use_broker_primary
            else f"  Broker Net Liq change          ${capital_delta_today:,.2f}  ({broker_return_pct:+.2f}% vs prior close)"
        ),
        (
            "    Use broker row above as headline account performance; compare trade sum for execution fidelity."
            if use_broker_primary
            else "    Trade-log primary mode: compare broker line below when validating vs IB."
        ),
        *(
            [
                f"  Avg broker daily delta (log)   ${avg_broker_daily_delta:,.2f}  "
                f"(mean NL step between dated snapshots through this report)"
            ]
            if equity_to_date
            else []
        ),
        "",
        *_report_section_heading("TODAY - BROKER EQUITY (daily_equity.csv) vs TRADE LOG (trades.csv)"),
        "  Broker line: Net Liq from each session row in daily_equity (last print of run, from IB).",
        f"  Prior close to session close   ${day_open_capital:,.2f} -> ${day_close_capital:,.2f}",
        f"  Broker day change (Net Liq)    ${capital_delta_today:,.2f}  (sessions logged: {n_sessions_today})",
        f"  Peak equity (day, from log)    ${day_peak_capital:,.2f}",
        "",
        f"  Closed-trade rows today        {n_trades_today}  (one row per partial TP1 and per final exit)",
        f"  Sum of booked P&L (rows)       ${daily_pnl:,.2f}",
        f"  Wins / losses (on row P&L)     {wins_today} / {losses_today}",
    ]
    if regime_exited is not None:
        lines.append(
            f"  Session counter trades_exited  {regime_exited}  (position exits; can be < trade rows if TP1 split)"
        )
    if regime_filled is not None and regime_filled != regime_exited:
        lines.append(f"  Session counter trades_filled  {regime_filled}")
    lines += [
        "",
        f"  Reconciliation gap             ${pnl_recon_gap:,.2f}   (= broker day change minus sum trade P&L)",
        "    Identity (always): broker day change = sum trade P&L + gap.",
        f"      Check: ${capital_delta_today:,.2f} = ${daily_pnl:,.2f} + (${pnl_recon_gap:,.2f})",
        "    If broker change is ~0 but trade sum is large, gap is ~negative trade sum by arithmetic",
        "      (not two unrelated mistakes). Means Net Liq snapshot did not move like the trade book.",
        "",
    ]
    if trade_row_issues:
        lines.append("  Trade-log quality flags (fix these or recon will not match broker):")
        for msg in trade_row_issues:
            lines.append(f"    - {msg}")
        lines.append("")
    lines += [
        *_report_section_heading("SNAPSHOT (cumulative, through report date)"),
        f"    Avg trade rows / trading day  {avg_trades_per_day:.2f}",
        f"    Win rate (today, row P&L)     {win_rate_today:.1f}%" if n_trades_today else "    Win rate (today)               n/a",
        f"    Return (broker / account)     {broker_return_pct:+.2f}%  (Net Liq change / prior close)",
        f"    Return (trade row model)      {day_return_pct:+.2f}%  (sum rows / prior close)",
        f"    Avg daily P&L (trade log)      ${roll['avg_daily_pnl']:,.2f}",
        f"    Best / worst day (by date)     {best_day} ${roll['best_pnl']:,.2f}  |  {worst_day} ${roll['worst_pnl']:,.2f}",
        f"    Max drawdown (model)          ${roll['max_dd']:,.2f} ({roll['max_dd_pct']:.1f}%)",
        f"    Exit reason mix (today)        {exit_mix_today}",
        "",
        *_report_section_heading("ROLLING / ALL-TIME (trade log + broker equity log)"),
        f"  Trading days with activity      {roll['n_days']}",
        f"  Total closed-trade rows         {roll['n_trades']}",
        f"  Sum of trade-log P&L            ${roll['total_pnl']:,.2f}",
        f"  Implied equity (1st snapshot + sum P&L)  ${roll_trade_implied:,.2f}  (ignores funding jumps)",
        f"  First broker snapshot            ${roll['broker_equity_first']:,.2f}  ({roll['broker_date_first']})",
        f"  Latest broker snapshot         ${roll['broker_equity_last']:,.2f}  ({roll['broker_date_last']})",
        f"  Return, trade sum / 1st snap     {roll['ret_pct']:.2f}%  (not meaningful if account was reset)",
        f"  Return, broker first-to-last     {roll_broker_ret_str}",
        f"  Avg daily P&L (trade log)      ${roll['avg_daily_pnl']:,.2f}",
        f"  Avg daily broker delta (log)  ${avg_broker_daily_delta:,.2f}  (mean Net Liq step, through this report)"
        if equity_to_date
        else f"  Avg daily broker delta (log)  n/a  (no equity history to this date)",
        f"  Best / worst day                {roll['best_date']}  ${roll['best_pnl']:,.2f}  |  {roll['worst_date']}  ${roll['worst_pnl']:,.2f}",
        f"  Max drawdown (model)            ${roll['max_dd']:,.2f} ({roll['max_dd_pct']:.1f}%)",
        f"  Win rate                        {roll['win_rate']:.1f}% ({roll['wins']} wins / {roll['losses']} losses)",
        "",
        *_report_section_heading("CLUSTERING & RISK"),
        f"  Worst 5-day rolling P&L      ${roll['worst_5d_pnl']:,.2f}  (ending {roll['worst_5d_end_date']})",
        f"  Max stops in one day         {roll['max_stops_one_day']} (approx: PnL <= -2.5%)",
        f"  Stops per day distribution   {roll['stops_dist_str']}",
        f"  Longest losing streak        {roll['longest_losing_streak']} trades  P&L ${roll['worst_streak_pnl']:,.2f}",
        f"  Worst 10 consecutive trades  P&L ${roll['worst_10_pnl']:,.2f}  ({roll['worst_10_pct']:.1f}% of capital at start of run)",
        f"  Theoretical one-day loss     {theoretical_one_day_pct:.1f}% of capital (all positions stop, before slippage)",
        "",
        *_report_section_heading("INTRADAY DRAWDOWN"),
        f"  Max intraday drawdown        ${roll['max_intraday_dd']:,.2f}",
        f"  Max single-day intraday DD  ${roll['max_single_day_intraday_dd']:,.2f}  on {roll['max_single_day_intraday_dd_date']}",
    ]

    stp_exits = int(exit_counts.get("STP", 0))
    if n_trades_today == 0:
        day_story = (
            f"No trades were executed on {date_only}. Booked closed-trade P&L is ${daily_pnl:,.2f} "
            "(0.0% win rate). This usually means gates, bias, or opportunity "
            "set did not justify new risk today."
        )
    else:
        day_story = (
            f"Daily broker move (Net Liq vs prior close): ${capital_delta_today:,.2f} ({broker_return_pct:+.2f}%). "
            f"Trade-log model sum: ${daily_pnl:,.2f} ({day_return_pct:+.2f}%) across {n_trades_today} closed-trade row(s)"
        )
        if decisive_today:
            day_story += (
                f", a {win_rate_today:.1f}% session win rate ({wins_today} wins / {losses_today} losses)"
            )
        day_story += f". Today's exit mix: {exit_mix_today}."
        if stp_exits:
            day_story += (
                f" {stp_exits} exit(s) tagged STP - compare to intraday path and the configured stop "
                "if you are diagnosing chop versus trend."
            )
    recon_tail = ""
    if trade_row_issues:
        recon_tail = (
            " Broker equity vs trade-log sum diverges until bad rows are corrected "
            f"(gap ${pnl_recon_gap:,.2f}). "
            + " ".join(trade_row_issues)
        )
    elif abs(pnl_recon_gap) >= 1.0:
        recon_tail = (
            f" Model vs account: broker day change (${capital_delta_today:,.2f}) minus trade row sum (${daily_pnl:,.2f}) "
            f"= ${pnl_recon_gap:,.2f} (this is not a random second error; it is the book-keeping difference). "
            " For headline P&L this report uses the broker (Net Liq) line in PRIMARY above. "
            "Gaps come from fills, fees, lot math, or imputed prices in the log. "
        )
        if abs(capital_delta_today) < 2.0 and abs(daily_pnl) > 5.0:
            recon_tail += (
                " If net liquidation is almost flat but trade rows show material P&L, compare to IB Trades/Activity; "
                "the log is a strategy book, not always equal to end-of-day broker net liquidation."
            )
    elif abs(pnl_recon_gap) >= 0.01:
        recon_tail = (
            f" Small reconciliation residual ${pnl_recon_gap:,.2f} "
            "(rounding, fees, or marks)."
        )
    decisive_all = roll["wins"] + roll["losses"]
    wr_all = f"{roll['win_rate']:.1f}% ({roll['wins']}W / {roll['losses']}L)" if decisive_all else "n/a (no decisive closes yet)"
    verdict_body = (
        "idle book - capital was not put into new closed trades; whether that was ideal depends on tape quality versus your edge."
        if n_trades_today == 0
        else (
            "repair trades.csv rows flagged above; analytics stay misleading until broker-truth exits are logged."
            if trade_row_issues
            else (
                "elevated stop participation vs. total exits - worth a quick scan for whipsaw or gap risk if that pattern repeats."
                if stp_exits >= max(2, (n_trades_today + 1) // 2)
                else "outcomes are fully summarized in the tables; use exit mix and intraday DD lines to judge regime fit."
            )
        )
    )
    lines.append("")
    lines.extend(_report_section_heading("NARRATIVE SUMMARY"))
    lines.extend(_wrap_report_prose(day_story + recon_tail))
    lines.append("")
    lines.extend(
        _wrap_report_prose(
            "Rolling context (through this report date, from logs above): "
            f"cumulative return {roll['ret_pct']:.2f}%, max drawdown ${roll['max_dd']:,.2f} "
            f"({roll['max_dd_pct']:.1f}%), all-time win rate {wr_all}, "
            f"avg daily P&L ${roll['avg_daily_pnl']:,.2f} across {roll['n_days']} trading day(s) with activity. "
            f"Calendar best {roll['best_date']} (${roll['best_pnl']:,.2f}); "
            f"worst {roll['worst_date']} (${roll['worst_pnl']:,.2f})."
        )
    )
    lines.append("")
    lines.extend(
        _wrap_report_prose(
            "Verdict (read with the numeric sections; not investment advice): " + verdict_body
        )
    )
    lines.append("")

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(_finalize_report_lines(lines)))
    except OSError:
        return None

    _mirror_daily_report_file(report_path)

    return report_path


def generate_backtest_report(
    all_trades: list[dict],
    date_from: str,
    date_to: str,
    start_capital: float,
    end_capital: float,
    agg_bias: dict | None = None,
    agg_condition: dict | None = None,
    signal_hits_long: int = 0,
    signal_hits_short: int = 0,
    events_processed: int = 0,
    trading_days_requested: int | None = None,
    trading_days_replayed: int | None = None,
    diag_summary: dict | None = None,
) -> str | None:
    """
    Generate backtest report. Saves to Reports/backtest_report_YYYY-MM-DD.txt or
    Reports/backtest_report_YYYY-MM-DD_to_YYYY-MM-DD.txt. Kept separate from live daily_report.
    """
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    date_range = f"{date_from}_to_{date_to}" if date_from != date_to else date_from
    report_path = os.path.join(config.REPORT_DIR, f"backtest_report_{date_range}.txt")

    # Convert backtest trade format to _rolling_stats format
    trades_for_stats = []
    for t in all_trades:
        try:
            pnl_d = float(t.get("pnl_dollars", 0))
            pnl_p = float(t.get("pnl_pct", 0))
        except (ValueError, TypeError):
            continue
        trades_for_stats.append({
            "Date": (t.get("date") or "")[:10],
            "PnL_Dollars": pnl_d,
            "PnL_Pct": pnl_p,
        })

    roll = _rolling_stats(trades_for_stats, [], start_capital)
    roll["final_capital"] = end_capital

    total_pnl = sum(t["PnL_Dollars"] for t in trades_for_stats)
    wins = sum(1 for t in trades_for_stats if t["PnL_Dollars"] > 0)
    losses = sum(1 for t in trades_for_stats if t["PnL_Dollars"] <= 0)
    total_trades = len(all_trades)
    replay_days = int(trading_days_replayed if trading_days_replayed is not None else roll.get("n_days", 0) or 0)
    req_days = int(trading_days_requested if trading_days_requested is not None else replay_days)
    avg_trades_per_day = (total_trades / replay_days) if replay_days > 0 else 0.0
    best_day = roll.get("best_date") or "N/A"
    worst_day = roll.get("worst_date") or "N/A"
    worst_5d_end = roll.get("worst_5d_end_date") or "N/A"
    fill_model = (diag_summary or {}).get("fill_model", "live_parity")
    spy_soft_penalties = int((diag_summary or {}).get("spy_soft_penalties", 0) or 0)
    conf_counts = (diag_summary or {}).get("confirmation_counts", {}) or {}
    conf_strict = int(conf_counts.get("strict", 0) or 0)
    conf_fast = int(conf_counts.get("fast_track", 0) or 0)

    # Exit mix for backtest trades.
    exit_counts: dict[str, int] = {}
    for t in all_trades:
        r = str(t.get("exit_reason") or "").upper() or "UNKNOWN"
        exit_counts[r] = exit_counts.get(r, 0) + 1
    exit_mix = ", ".join(f"{k}:{v}" for k, v in sorted(exit_counts.items())) if exit_counts else "N/A"

    theoretical_one_day_pct = (
        getattr(config, "MAX_POSITIONS", 10)
        * getattr(config, "MAX_POSITION_PCT", 0.15)
        * getattr(config, "STOP_PCT", 0.03)
        * 100
    )

    agg_bias = agg_bias or {}
    agg_condition = agg_condition or {}

    bt_title = f"  Fast Paper Long/Short – BACKTEST Report  {date_from} to {date_to}"
    if len(bt_title) > REPORT_LINE_WIDTH:
        bt_title = bt_title[: REPORT_LINE_WIDTH - 1].rstrip() + "…"

    lines = [
        _report_ruler("="),
        bt_title,
        _report_ruler("="),
        "",
        "  *** BACKTEST – not live trading ***",
        "",
        f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        *_report_section_heading("BACKTEST RANGE"),
        f"  Date from                     {date_from}",
        f"  Date to                       {date_to}",
        f"  Trading days requested        {req_days}",
        f"  Trading days replayed         {replay_days}",
        "",
        "  Capital / position rules",
        f"    Max positions per day         {getattr(config, 'MAX_POSITIONS', 10)}",
        f"    Max position per trade        {getattr(config, 'MAX_POSITION_PCT', 0.15) * 100:.0f}% of capital",
        f"    Entry order timeout           {getattr(config, 'ENTRY_ORDER_TIMEOUT_SECONDS', 900) // 60} min",
        "",
        "  Exit rules",
        f"    TP1                          {getattr(config, 'PARTIAL_TP_PCT', 0.015) * 100:.1f}%"
        f"  x{getattr(config, 'PARTIAL_TP_FRACTION', 0.5) * 100:.0f}%",
        f"    Runner cap                   {getattr(config, 'RUNNER_CAP_PCT', 0.065) * 100:.0f}%",
        f"    Stop                         {getattr(config, 'STOP_PCT', 0.025) * 100:.0f}%",
        "",
        *_report_section_heading("BACKTEST RESULTS"),
        f"  Total trades                  {total_trades}",
        f"  Avg trades / replay day       {avg_trades_per_day:.2f}",
        f"  Wins / Losses                 {wins} / {losses}",
        f"  Win rate                      {(wins / total_trades * 100) if total_trades else 0:.1f}%",
        f"  Start capital                 ${start_capital:,.2f}",
        f"  End capital                   ${end_capital:,.2f}",
        f"  Total P&L                     ${total_pnl:,.2f}",
        f"  Return                        {roll['ret_pct']:.2f}%",
        f"  Avg daily P&L                 ${roll['avg_daily_pnl']:,.2f}",
        f"  Best day                      {best_day}  P&L ${roll['best_pnl']:,.2f}",
        f"  Worst day                     {worst_day}  P&L ${roll['worst_pnl']:,.2f}",
        f"  Max drawdown                  ${roll['max_dd']:,.2f} ({roll['max_dd_pct']:.1f}%)",
        f"  Exit reason mix               {exit_mix}",
        "",
        *_report_section_heading("CLUSTERING & RISK"),
        f"  Worst 5-day rolling P&L       ${roll['worst_5d_pnl']:,.2f}  (ending {worst_5d_end})",
        f"  Max stops in one day          {roll['max_stops_one_day']} (approx: PnL <= -2.5%)",
        f"  Stops per day distribution    {roll['stops_dist_str']}",
        f"  Longest losing streak         {roll['longest_losing_streak']} trades  P&L ${roll['worst_streak_pnl']:,.2f}",
        f"  Worst 10 consecutive trades   P&L ${roll['worst_10_pnl']:,.2f}  ({roll['worst_10_pct']:.1f}%)",
        f"  Theoretical one-day loss      {theoretical_one_day_pct:.1f}% of capital",
        "",
        *_report_section_heading("DIAGNOSTICS"),
        f"  Bias distribution             LONG: {agg_bias.get('LONG', 0)}, SHORT: {agg_bias.get('SHORT', 0)}, NEUTRAL: {agg_bias.get('NEUTRAL', 0)}",
        f"  Signal hits (long / short)    {signal_hits_long} / {signal_hits_short}",
        f"  Confirmations (strict/fast)   {conf_strict} / {conf_fast}",
        f"  SPY soft penalties            {spy_soft_penalties}",
        f"  Fill model                    {fill_model}",
        f"  Events processed              {events_processed}",
        f"  Per-condition hits            {agg_condition}",
    ]

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(_finalize_report_lines(lines)))
        return report_path
    except OSError:
        return None

