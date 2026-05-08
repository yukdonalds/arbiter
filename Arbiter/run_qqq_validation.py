#!/usr/bin/env python3
"""
Run three backtest modes for QQQ validation (requires IBKR connection + cached/historical data).
Writes Reports/qqq_intraday_validation_<from>_to_<to>.txt
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from collections import Counter

import config

DATE_FROM = "2026-04-20"
DATE_TO = "2026-05-01"


def _sum_pnl(rows: list[dict]) -> float:
    return sum(float(r.get("pnl_dollars") or 0) for r in rows)


def _load_csv(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append_qqq_diag_json(lines: list[str], mode_key: str, rng: str) -> None:
    """Reads logs/qqq_backtest_session_diag_<mode>_<rng>.json from the last subprocess run."""
    primary = os.path.join(config.LOG_DIR, f"qqq_backtest_session_diag_{mode_key}_{rng}.json")
    alt_rng = rng.replace("_to_", "_")
    alt = os.path.join(config.LOG_DIR, f"qqq_backtest_session_diag_{mode_key}_{alt_rng}.json")

    path = primary if os.path.isfile(primary) else alt
    if not os.path.isfile(path):
        lines.append(f"(no {primary} / {alt})")
        return
    try:
        with open(path, encoding="utf-8") as jf:
            data = json.load(jf)
    except Exception as e:
        lines.append(f"(failed to read QQQ diagnostics JSON: {e})")
        return
    totals = data.get("totals") or {}
    lines.append("Aggregate totals (all sessions in range):")
    for k in (
        "qqq_bars_received",
        "qqq_regime_pass_count",
        "qqq_regime_fail_count",
        "qqq_trend_up_bars",
        "qqq_trend_down_bars",
        "qqq_chop_bars",
        "qqq_vwap_setup_count",
        "qqq_entry_trigger_count",
        "qqq_trades_filled",
        "qqq_trade_count",
    ):
        lines.append(f"  {k}: {totals.get(k, 0)}")
    hist = data.get("regime_fail_histogram") or {}
    if hist:
        lines.append(f"  regime_fail_histogram: {dict(hist)}")
    sessions = data.get("sessions") or []
    lines.append(f"Per-session rows: {len(sessions)}")
    for s in sessions[:20]:
        sd = s.get("session_date", "")
        or_pct = float(s.get("opening_range_pct") or 0.0)
        atr5 = float(s.get("qqq_5min_atr_pct") or 0.0)
        vd = float(s.get("vwap_distance_pct") or 0.0)
        lines.append(
            f"  {sd}: OR%={or_pct:.3f} ATR5%={atr5:.3f} "
            f"vwap_dist%={vd:.3f} pass_last={s.get('regime_pass_last_bar')} "
            f"fail_reason={s.get('regime_fail_reason_last')!r} "
            f"trend_up={s.get('qqq_trend_up_bars')} trend_down={s.get('qqq_trend_down_bars')} chop={s.get('qqq_chop_bars')} "
            f"vwap={s.get('qqq_vwap_setup_count')} entries={s.get('qqq_entry_trigger_count')} fills={s.get('qqq_trades_filled')}"
        )
    if len(sessions) > 20:
        lines.append(f"  ... ({len(sessions) - 20} more sessions omitted)")


def _stats(trades: list[dict], label: str, lines: list[str]) -> None:
    lines.append(f"\n{'=' * 60}")
    lines.append(label)
    lines.append(f"{'=' * 60}")
    if not trades:
        lines.append("(no trades)")
        return
    pnl = _sum_pnl(trades)
    wins = sum(1 for t in trades if float(t.get("pnl_dollars") or 0) > 0)
    lines.append(f"Trades (rows): {len(trades)}")
    lines.append(f"Win rate:      {wins / len(trades) * 100:.1f}%")
    lines.append(f"Total P&L:     ${pnl:,.2f}")
    lines.append(f"Avg trade:     ${pnl / len(trades):,.2f}")
    mfe_vals = [float(t.get("MFE_pct") or 0) for t in trades if str(t.get("MFE_pct") or "").strip() != ""]
    mae_vals = [float(t.get("MAE_pct") or 0) for t in trades if str(t.get("MAE_pct") or "").strip() != ""]
    if mfe_vals:
        lines.append(f"Avg MFE:       {sum(mfe_vals) / len(mfe_vals):.3f}%")
    if mae_vals:
        lines.append(f"Avg MAE:       {sum(mae_vals) / len(mae_vals):.3f}%")
    reasons = Counter(str(t.get("exit_reason") or "") for t in trades)
    lines.append(f"Exit reasons:  {dict(reasons)}")
    setups = Counter(str(t.get("setup_type") or "") for t in trades)
    lines.append(f"Setup mix:     {dict(setups)}")


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    py = sys.executable
    rng = f"{DATE_FROM}_to_{DATE_TO}"
    report_path = os.path.join(config.REPORT_DIR, f"qqq_intraday_validation_{rng}.txt")

    runs = [
        ("arbiter-only", ["--arbiter-only"]),
        ("qqq-only", ["--qqq-only"]),
        ("combined", ["--combined"]),
    ]

    lines: list[str] = []
    lines.append("QQQ INTRADAY VALIDATION REPORT")
    lines.append(f"Range: {DATE_FROM} .. {DATE_TO}")
    lines.append("")
    lines.append("Each section ran backtest_fpls_yesterday.py in a separate subprocess.")
    lines.append("Sections: STOCK ARBITER RESULTS | QQQ INTRADAY RESULTS | COMBINED RESULTS | QQQ DIAGNOSTICS")

    stock_csv = os.path.join(config.LOG_DIR, f"backtest_trades_{DATE_FROM}_{DATE_TO}.csv")
    qqq_csv = os.path.join(config.LOG_DIR, f"qqq_backtest_trades_{DATE_FROM}_{DATE_TO}.csv")

    archived_stock: dict[str, str] = {}
    archived_qqq: dict[str, str] = {}

    for name, extra in runs:
        cmd = [
            py,
            os.path.join(base, "backtest_fpls_yesterday.py"),
            "--from",
            DATE_FROM,
            "--to",
            DATE_TO,
            *extra,
        ]
        lines.append(f"\n--- subprocess: {name} ---")
        lines.append(" ".join(cmd))
        r = subprocess.run(
            cmd,
            cwd=base,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines.append(f"exit_code={r.returncode}")
        tail = (r.stdout or "").splitlines()[-40:]
        if tail:
            lines.append("stdout (tail):")
            lines.extend(tail)
        if r.stderr:
            lines.append("stderr:")
            lines.append(r.stderr[-4000:])

        key = name.replace("-", "_")
        if os.path.isfile(stock_csv):
            dest = os.path.join(config.LOG_DIR, f"backtest_trades_{key}_{DATE_FROM}_{DATE_TO}.csv")
            shutil.copy2(stock_csv, dest)
            archived_stock[name] = dest
        if os.path.isfile(qqq_csv):
            qdest = os.path.join(config.LOG_DIR, f"qqq_backtest_trades_{key}_{DATE_FROM}_{DATE_TO}.csv")
            shutil.copy2(qqq_csv, qdest)
            archived_qqq[name] = qdest

    lines.append("\nArchived CSV copies:")
    for k, p in archived_stock.items():
        lines.append(f"  stock [{k}]: {p}")
    for k, p in archived_qqq.items():
        lines.append(f"  qqq   [{k}]: {p}")

    stock_rows_arb = _load_csv(archived_stock.get("arbiter-only") or "")
    stock_rows_comb = _load_csv(archived_stock.get("combined") or "")
    qqq_rows_only = _load_csv(archived_qqq.get("qqq-only") or "")
    qqq_rows_comb = _load_csv(archived_qqq.get("combined") or "")

    _stats(stock_rows_arb, "STOCK ARBITER RESULTS (--arbiter-only)", lines)
    _stats(qqq_rows_only, "QQQ INTRADAY RESULTS (--qqq-only)", lines)

    comb_stock = stock_rows_comb
    comb_pnl_stock = _sum_pnl(comb_stock)
    comb_pnl_qqq = _sum_pnl(qqq_rows_comb)
    lines.append("\n" + "=" * 60)
    lines.append("COMBINED RESULTS (--combined)")
    lines.append("=" * 60)
    lines.append(f"Stock P&L: ${comb_pnl_stock:,.2f}  (n={len(comb_stock)})")
    lines.append(f"QQQ P&L:   ${comb_pnl_qqq:,.2f}  (n={len(qqq_rows_comb)})")
    lines.append(f"Sum P&L:   ${comb_pnl_stock + comb_pnl_qqq:,.2f}")

    freq_stock = len(stock_rows_arb)
    freq_qqq = len(qqq_rows_only)
    freq_comb = len(comb_stock) + len(qqq_rows_comb)
    lines.append("")
    lines.append(
        f"Trade-frequency proxy (CSV rows): arbiter-only stock={freq_stock}, "
        f"qqq-only legs={freq_qqq}, combined stock+qqq legs={freq_comb}"
    )
    lines.append(
        f"QQQ added legs vs stock-only run: combined_rows - arbiter_stock_rows = {len(comb_stock) + len(qqq_rows_comb) - freq_stock} "
        f"(approximate; partial exits create multiple rows)."
    )

    lines.append("\n" + "=" * 60)
    lines.append("QQQ DIAGNOSTICS (from qqq_backtest_session_diag_*.json)")
    lines.append("=" * 60)
    lines.append("\n--qqq-only session diagnostics--")
    _append_qqq_diag_json(lines, "qqq", rng)
    lines.append("\n--combined session diagnostics (QQQ leg only; SPY/stock unchanged)--")
    _append_qqq_diag_json(lines, "combined", rng)

    os.makedirs(config.REPORT_DIR, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write("\n".join(lines) + "\n")
    print(report_path)


if __name__ == "__main__":
    main()
