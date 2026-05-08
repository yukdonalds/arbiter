from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
ARBITER_DIR = ROOT / "Arbiter"
if str(ARBITER_DIR) not in sys.path:
    sys.path.insert(0, str(ARBITER_DIR))

import backtest_fpls_yesterday as bt  # noqa: E402


def _fetch_day_inputs(day_str: str):
    day = datetime.strptime(day_str, "%Y-%m-%d").date()
    end_dt = bt.EASTERN.localize(
        datetime.combine(day, datetime.min.time().replace(hour=16, minute=0, second=0, microsecond=0))
    )
    end_dt_str = bt._end_dt_str(end_dt)

    use_fixed = getattr(bt.config, "USE_FIXED_UNIVERSE", True)
    top_n = int(getattr(bt.config, "WATCHLIST_TOP_N", 150))
    if use_fixed:
        tickers = bt.load_fixed_universe_100()
    else:
        tickers = bt.load_all_sp500_tickers()

    ib = bt.connect_ib()
    try:
        metrics_end_dt = bt._prior_trading_day_close_et(end_dt)
        prior_date = metrics_end_dt.date()
        metrics_end_dt_str = bt._end_dt_str(metrics_end_dt)

        if use_fixed:
            daily_metrics = bt.fetch_daily_metrics_parallel_for_date(
                ib,
                tickers,
                metrics_end_dt_str,
                max_tickers=len(tickers),
                use_backtest_volume_min=False,
                apply_filters=False,
                progress_callback=None,
            )
            tickers_day = [s for s in tickers if s in daily_metrics]
        else:
            tickers_day, daily_metrics = bt.rescan_universe_for_day(
                prior_date,
                ib,
                tickers,
                top_n,
                progress_callback=None,
            )

        max_subs = int(getattr(bt.config, "MAX_REALTIME_SUBSCRIPTIONS", 100))
        subs = list(tickers_day[:max_subs])
        if bt.ETF_SYMBOL not in subs:
            if len(subs) >= max_subs:
                subs = subs[:-1] + [bt.ETF_SYMBOL]
            else:
                subs.append(bt.ETF_SYMBOL)
        tickers_day = [t for t in subs if t != bt.ETF_SYMBOL]

        all_bars = bt.fetch_5sec_bars_parallel(ib, subs, end_dt_str, day_str)
    finally:
        bt.disconnect_ib(ib)

    etf_bars = all_bars.get(bt.ETF_SYMBOL, [])
    ticker_bars = {s: all_bars.get(s, []) for s in tickers_day}
    if getattr(bt.config, "BACKTEST_IB_5SEC_ONLY", False):
        ticker_bars = {s: b for s, b in ticker_bars.items() if b and bt.bars_look_like_ib_5sec_TRADES(b)}
    tickers_day = [s for s in tickers_day if ticker_bars.get(s)]
    dm_day = {s: daily_metrics[s] for s in tickers_day if s in daily_metrics}
    events = bt.build_event_stream(etf_bars, {s: ticker_bars[s] for s in tickers_day})
    return events, dm_day


def _enrich_with_forward_metrics(strict_records: list[dict]) -> None:
    yf_cache: dict[tuple[str, object], object] = {}
    for rec in strict_records:
        ts_obj = rec.get("timestamp_obj")
        if not isinstance(ts_obj, datetime):
            try:
                ts_obj = bt.EASTERN.localize(
                    datetime.strptime(str(rec.get("timestamp_et") or ""), "%Y-%m-%d %H:%M:%S")
                )
            except Exception:
                continue
        key = (str(rec.get("ticker") or "").upper(), ts_obj.date())
        if key not in yf_cache:
            yf_cache[key] = bt._fetch_forensic_yf_1m(key[0], key[1])
        metrics = bt._forensic_metrics_after_confirmation(
            str(rec.get("side") or "LONG"),
            float(rec.get("confirmation_price") or 0.0),
            ts_obj,
            yf_cache.get(key),
        )
        rec.update(metrics)


def _fmt_pct(v: float) -> str:
    try:
        return f"{float(v):.3f}%"
    except Exception:
        return "n/a"


def run_forensic(day_str: str) -> Path:
    events, dm_day = _fetch_day_inputs(day_str)
    trades, _, _, diag = bt.run_backtest(events, dm_day, start_capital=100_000.0, date_str=day_str)
    strict_records = list(diag.get("strict_confirmation_records") or [])
    _enrich_with_forward_metrics(strict_records)

    executed = [r for r in strict_records if bool(r.get("became_trade"))]
    missed = [r for r in strict_records if not bool(r.get("became_trade"))]

    reasons = Counter(str(r.get("block_reason") or "other") for r in missed)
    already_or_pending = (
        reasons.get("already_in_position", 0)
        + reasons.get("same_ticker_reentry_day_blocked", 0)
        + reasons.get("max_positions_limit", 0)
    )
    covered = {
        "entry_cutoff_blocked",
        "late_entry_blocked",
        "entry_strength_failed",
        "already_in_position",
        "same_ticker_reentry_day_blocked",
        "max_positions_limit",
    }
    other_count = sum(v for k, v in reasons.items() if k not in covered)

    mfe_1 = sum(1 for r in missed if float(r.get("mfe_pct") or 0.0) >= 1.0)
    mfe_15 = sum(1 for r in missed if float(r.get("mfe_pct") or 0.0) >= 1.5)
    mae_and_mfe = sum(
        1
        for r in missed
        if float(r.get("mae_pct") or 0.0) <= -1.0 and float(r.get("mfe_pct") or 0.0) >= 1.0
    )

    missed_sorted = sorted(missed, key=lambda r: float(r.get("mfe_pct") or -9999.0), reverse=True)
    top5 = missed_sorted[:5]

    avg_mfe = (sum(float(r.get("mfe_pct") or 0.0) for r in missed) / len(missed)) if missed else 0.0
    avg_mae = (sum(float(r.get("mae_pct") or 0.0) for r in missed) / len(missed)) if missed else 0.0
    avg_score = (sum(float(r.get("score") or 0.0) for r in missed) / len(missed)) if missed else 0.0

    lines: list[str] = []
    lines.append("========================================")
    lines.append(f"FORENSIC ANALYSIS — {day_str}")
    lines.append("========================================")
    lines.append("")
    lines.append("SUMMARY")
    lines.append("----------------------------------------")
    lines.append(f"Total Candidates: {len(strict_records)}")
    lines.append(f"Strict Confirmations: {len(strict_records)}")
    lines.append(f"Executed Trades: {len(executed)}")
    lines.append(f"Missed Opportunities: {len(missed)}")
    lines.append("")
    lines.append("BLOCK REASONS")
    lines.append("----------------------------------------")
    lines.append(f"entry_cutoff_blocked: {reasons.get('entry_cutoff_blocked', 0)}")
    lines.append(f"late_entry_blocked: {reasons.get('late_entry_blocked', 0)}")
    lines.append(f"entry_strength_failed: {reasons.get('entry_strength_failed', 0)}")
    lines.append(f"already_in_position_or_pending: {already_or_pending}")
    lines.append(f"other: {other_count}")
    lines.append("")
    lines.append("MISSED TRADE QUALITY")
    lines.append("----------------------------------------")
    lines.append(f"MFE >= 1.0%: {mfe_1}")
    lines.append(f"MFE >= 1.5%: {mfe_15}")
    lines.append(f"MAE <= -1.0% AND MFE >= 1.0%: {mae_and_mfe}")
    lines.append("")
    lines.append("TOP MISSED TRADES (by MFE)")
    lines.append("----------------------------------------")
    if top5:
        for i, r in enumerate(top5, start=1):
            lines.append(
                f"{i}. {str(r.get('ticker') or '')} {str(r.get('side') or '')} | "
                f"Score: {float(r.get('score') or 0.0):.3f} | "
                f"MFE: {_fmt_pct(float(r.get('mfe_pct') or 0.0))} | "
                f"MAE: {_fmt_pct(float(r.get('mae_pct') or 0.0))} | "
                f"Block: {str(r.get('block_reason') or 'other')}"
            )
    else:
        lines.append("1. n/a")
        lines.append("2. n/a")
        lines.append("3. n/a")
        lines.append("4. n/a")
        lines.append("5. n/a")
    lines.append("")
    lines.append("DETAILS (OPTIONAL BUT INCLUDE IF EASY)")
    lines.append("----------------------------------------")
    lines.append(f"- Average MFE of missed trades: {_fmt_pct(avg_mfe)}")
    lines.append(f"- Average MAE of missed trades: {_fmt_pct(avg_mae)}")
    lines.append(f"- Average score of missed trades: {avg_score:.3f}")

    out_dir = ROOT / "analysis" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"forensic_{day_str}.txt"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


if __name__ == "__main__":
    output = run_forensic("2026-05-07")
    print(f"Wrote {output}")
