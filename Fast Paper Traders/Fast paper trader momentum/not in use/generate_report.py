# -----------------------------
# Fast Paper Trader – Post-session report generator
# -----------------------------
"""
Generate a daily report from log files and optionally from IB API.

Usage:
  python generate_report.py                    # Today, offline (logs only)
  python generate_report.py --date 2026-02-19  # Specific date, offline
  python generate_report.py --date 2026-02-19  # With TWS running: add IB data
  python generate_report.py --offline          # Force offline (no IB connection)

Offline: reads logs/trades.csv, logs/daily_equity.csv. Works after everything is shut down.
Online: connect to TWS (127.0.0.1:7497), fetch executions and positions for the date.
"""
import argparse
from datetime import datetime

# Import report logic from daily_report
from daily_report import (
    _load_trades,
    _load_equity,
    _rolling_stats,
    generate_daily_report as _generate_from_logs,
)
from trade_logger import _trade_log_path, init_trade_log


def _trades_from_ib_executions(executions: list, target_date: str | None = None) -> list[dict]:
    """
    Derive round-trip trades from IB executions. Pairs BUY+SELL by symbol (FIFO).
    Filters to stocks only (excludes options, non-stocks). Returns list of {symbol, shares, entry, exit, pnl, pnl_pct, Date}.
    If target_date is set, only include trades for that date.
    """
    target = target_date[:10].replace("-", "") if target_date else None
    buys = []
    sells = []
    for e in executions or []:
        sym = (e.get("symbol") or "").strip()
        sec_type = (e.get("secType") or "STK").upper()
        if sec_type not in ("STK", "STOCK", ""):
            continue
        if len(sym) > 6 or "." in sym or sym.endswith(".USD"):
            continue
        side = (e.get("side") or "").upper()
        shares = float(e.get("shares", 0) or 0)
        price = float(e.get("price", 0) or 0)
        dt = e.get("time", "")
        if hasattr(dt, "strftime"):
            dt = dt.strftime("%Y-%m-%d %H:%M:%S")
        dt_norm = str(dt)[:10].replace("-", "") if dt else ""
        if target and dt_norm != target:
            continue
        if side in ("BUY", "BOT") and shares > 0:
            buys.append({"symbol": sym, "shares": shares, "price": price, "time": dt, "date_str": dt_norm})
        elif side in ("SELL", "SLD") and shares > 0:
            sells.append({"symbol": sym, "shares": shares, "price": price, "time": dt, "date_str": dt_norm})

    trades = []
    by_sym_date = {}
    for b in buys:
        key = (b["symbol"], b.get("date_str", ""))
        by_sym_date.setdefault(key, []).append({"shares": b["shares"], "price": b["price"], "side": "B", "time": b.get("time", ""), "date_str": b.get("date_str", "")})
    for s in sells:
        key = (s["symbol"], s.get("date_str", ""))
        by_sym_date.setdefault(key, []).append({"shares": s["shares"], "price": s["price"], "side": "S", "time": s.get("time", ""), "date_str": s.get("date_str", "")})

    for (sym, date_str), events in by_sym_date.items():
        events.sort(key=lambda x: (str(x.get("time", "")), 0 if x.get("side") == "B" else 1))
        q_buy = []
        q_sell = []
        for ev in events:
            sh, pr = ev["shares"], ev["price"]
            if (ev.get("side") or "").upper() == "B":
                q_buy.append((sh, pr))
            else:
                q_sell.append((sh, pr))
        while q_buy and q_sell:
            b_sh, b_pr = q_buy.pop(0)
            s_sh, s_pr = q_sell.pop(0)
            m = min(b_sh, s_sh)
            pnl = m * (s_pr - b_pr)
            pnl_pct = (s_pr / b_pr - 1) * 100 if b_pr else 0
            # Format date as YYYY-MM-DD
            d = date_str if date_str and len(date_str) >= 8 else ""
            if len(d) == 8:
                d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            trades.append({"symbol": sym, "shares": m, "entry": b_pr, "exit": s_pr, "pnl": pnl, "pnl_pct": pnl_pct, "Date": d})
            if b_sh > m:
                q_buy.insert(0, (b_sh - m, b_pr))
            if s_sh > m:
                q_sell.insert(0, (s_sh - m, s_pr))
    return trades


def _ib_trade_to_log_row(t: dict, date_str: str) -> dict:
    """Convert IB-derived trade to log format (Date, Ticker, PnL_Dollars, etc)."""
    return {
        "Date": date_str[:10],
        "Ticker": t.get("symbol", ""),
        "EntryTime": "",
        "EntryPrice": t.get("entry", 0),
        "Shares": t.get("shares", 0),
        "Target": 0,
        "Stop": 0,
        "ExitTime": "",
        "ExitPrice": t.get("exit", 0),
        "PnL_Dollars": t.get("pnl", 0),
        "PnL_Pct": t.get("pnl_pct", 0),
    }


def _append_ib_trades_to_csv(ib_trades: list, date_str: str) -> None:
    """Append IB-derived trades to trades.csv (backfill when app missed exits)."""
    if not ib_trades:
        return
    init_trade_log()
    import csv
    path = _trade_log_path()
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for t in ib_trades:
                row = _ib_trade_to_log_row(t, t.get("Date", date_str))
                w.writerow([
                    row["Date"], row["Ticker"], row["EntryTime"], row["EntryPrice"],
                    row["Shares"], row["Target"], row["Stop"], row["ExitTime"],
                    row["ExitPrice"], round(row["PnL_Dollars"], 2), round(row["PnL_Pct"], 4),
                ])
    except OSError:
        pass


def _equity_for_date(equity_rows: list, date_str: str) -> tuple[float, float, float]:
    """Return (day_start, day_end, day_peak) from equity logs for date."""
    date_only = date_str[:10]
    today = [r for r in equity_rows if (r.get("Date") or "")[:10] == date_only]
    if not today:
        return 0.0, 0.0, 0.0
    start = float(today[0].get("Equity", 0))
    end = float(today[-1].get("Equity", 0))
    peak = max(float(r.get("Peak", 0)) for r in today)
    return start, end, peak


def _fetch_ib_data(date_str: str, from_date: str | None = None, all_dates: bool = False) -> dict | None:
    """
    Connect to IB, fetch executions and positions.
    from_date: start time for request (yyyymmdd). Default: date_str.
    all_dates: if True, return all parsed executions; else filter to date_str only.
    """
    try:
        from ib_insync import IB, ExecutionFilter
        from ib_connection import connect_ib, get_account_value, disconnect_ib
    except ImportError:
        return None

    ib = None
    try:
        ib = connect_ib()
        account_id = ib.managedAccounts()[0] if ib.managedAccounts() else ""
        if not account_id:
            return None

        net_liq = get_account_value(ib, account_id)

        # Executions from from_date (format yyyymmdd hh:mm:ss)
        use_from = (from_date or date_str)[:10].replace("-", "")
        time_str = use_from + " 00:00:00"
        req = ExecutionFilter(time=time_str)
        if account_id:
            req.acctCode = account_id
        executions = ib.reqExecutions(req)
        ib.sleep(2)

        # Parse Fill objects
        target_date = None if all_dates else date_str[:10].replace("-", "")
        execs_list = []
        for fill in executions or []:
            ex = getattr(fill, "execution", None) if hasattr(fill, "execution") else fill
            if ex is None:
                continue
            dt = getattr(ex, "time", None) or getattr(fill, "time", None)
            dt_str = ""
            if hasattr(dt, "strftime"):
                dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(dt, str):
                dt_str = str(dt)[:19]
            else:
                continue
            dt_norm = dt_str[:10].replace("-", "")
            if target_date and dt_norm != target_date:
                continue
            sym = ""
            sec_type = "STK"
            c = getattr(fill, "contract", None)
            if c:
                sym = getattr(c, "symbol", "") or ""
                sec_type = getattr(c, "secType", "STK") or "STK"
            side = getattr(ex, "side", "") or ""
            shares = float(getattr(ex, "shares", 0) or getattr(ex, "cumQty", 0) or 0)
            price = float(getattr(ex, "avgPrice", 0) or getattr(ex, "price", 0) or 0)
            execs_list.append({
                "symbol": sym,
                "side": side,
                "shares": shares,
                "price": price,
                "time": dt_str,
                "secType": sec_type,
            })

        # Positions (Position: account, contract, position, avgCost)
        all_pos = ib.positions()
        pos_list = []
        for p in all_pos or []:
            if getattr(p, "account", "") and account_id and p.account != account_id:
                continue
            c = getattr(p, "contract", None)
            if not c:
                continue
            sym = getattr(c, "symbol", "")
            pos = int(getattr(p, "position", 0) or 0)
            avg_cost = float(getattr(p, "avgCost", 0) or 0)
            if sym and pos != 0:
                pos_list.append({"symbol": sym, "position": pos, "avgCost": avg_cost})

        disconnect_ib(ib)
        return {
            "net_liquidation": net_liq,
            "executions": execs_list,
            "positions": pos_list,
        }
    except Exception:
        if ib:
            try:
                disconnect_ib(ib)
            except Exception:
                pass
        return None


def _format_ib_section(ib_data: dict, date_str: str) -> list[str]:
    """Format IB supplemental section for report."""
    lines = [
        "",
        "------------------------------------------------------------------------",
        "  IB API SUPPLEMENT (from TWS – run with TWS open for this section)",
        "------------------------------------------------------------------------",
        "",
        f"  NetLiquidation (now)        ${ib_data.get('net_liquidation', 0):,.2f}",
        "",
    ]

    execs_all = ib_data.get("executions") or []
    target = date_str[:10].replace("-", "")
    execs = [e for e in execs_all if (str(e.get("time", ""))[:10].replace("-", "") == target)]
    if execs:
        lines.append("  Executions today:")
        for e in execs:
            lines.append(f"    {e.get('time', '')}  {e.get('side', '')} {e.get('symbol', '')}  "
                        f"{e.get('shares', 0)} @ {e.get('price', 0):.2f}")
        lines.append("")
    else:
        lines.append("  Executions today           (none or not requested)")
        lines.append("")

    poss = ib_data.get("positions") or []
    if poss:
        lines.append("  Open positions:")
        for p in poss:
            lines.append(f"    {p.get('symbol', '')}  {p.get('position', 0)} @ avg {p.get('avgCost', 0):.2f}")
        lines.append("")
    else:
        lines.append("  Open positions              (none)")
        lines.append("")

    return lines


def run_report(date_str: str, offline: bool) -> str | None:
    """Generate report for date_str. Returns report path or None."""
    equity_rows = _load_equity()
    log_trades = _load_trades()

    day_start, day_end, day_peak = _equity_for_date(equity_rows, date_str)
    if day_start == 0 and day_end == 0 and equity_rows:
        prev = [r for r in equity_rows if (r.get("Date") or "")[:10] < date_str[:10]]
        if prev:
            day_start = day_end = float(prev[-1].get("Equity", 0))
            day_peak = float(prev[-1].get("Peak", day_start))
    if day_start == 0:
        day_start = 2800.0
    if day_end == 0:
        day_end = day_start
    if day_peak == 0:
        day_peak = max(day_start, day_end)

    ib_data = None
    daily_stats_override = None
    trades_override = None
    date_only = date_str[:10]

    if not offline:
        # Fetch IB executions from start of year (for rolling stats)
        year_start = date_only[:4] + "-01-01"
        ib_data = _fetch_ib_data(date_str, from_date=year_start, all_dates=True)
        if ib_data and ib_data.get("executions"):
            ib_trades_all = _trades_from_ib_executions(ib_data["executions"], target_date=None)
            ib_trades_today = [t for t in ib_trades_all if (t.get("Date") or "")[:10] == date_only]
            if ib_trades_today:
                pnl = sum(t["pnl"] for t in ib_trades_today)
                wins = sum(1 for t in ib_trades_today if t["pnl"] > 0)
                losses = sum(1 for t in ib_trades_today if t["pnl"] < 0)
                daily_stats_override = {
                    "n_trades": len(ib_trades_today),
                    "pnl": pnl,
                    "wins": wins,
                    "losses": losses,
                }
                # Append to trades.csv when logs had no data for this date (backfill)
                if date_only not in {(t.get("Date") or "")[:10] for t in log_trades}:
                    _append_ib_trades_to_csv(ib_trades_today, date_only)

            # Merge for rolling: use log trades; add IB trades for dates with no log data
            log_dates = {(t.get("Date") or "")[:10] for t in log_trades}
            combined = list(log_trades)
            for t in ib_trades_all:
                d = (t.get("Date") or "")[:10]
                if d and d not in log_dates:
                    combined.append(_ib_trade_to_log_row(t, d))
            trades_override = combined

    report_path = _generate_from_logs(
        date_str, day_start, day_end, day_peak,
        ib_data=ib_data,
        daily_stats_override=daily_stats_override,
        trades_override=trades_override,
    )
    if not report_path:
        return None

    if ib_data:
        extra = _format_ib_section(ib_data, date_str)
        try:
            with open(report_path, "a", encoding="utf-8") as f:
                f.write("\n".join(extra))
        except OSError:
            pass

    return report_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate Fast Paper Trader daily report from logs (and IB if TWS is running)"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use logs only; do not connect to IB",
    )
    args = parser.parse_args()

    if args.date:
        date_str = args.date[:10]
    else:
        now = datetime.now()
        day = input("Day: ").strip() or str(now.day)
        month = input(f"Month [{now.month}]: ").strip() or str(now.month)
        year = input(f"Year [{now.year}]: ").strip() or str(now.year)
        date_str = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    mode = "offline (logs only)" if args.offline else "online (logs + IB if connected)"
    print(f"Generating report for {date_str} ({mode})...")

    path = run_report(date_str, args.offline)
    if path:
        print(f"Report saved: {path}")
    else:
        print("Report generation failed.")


if __name__ == "__main__":
    main()

