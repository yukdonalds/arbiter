# Rebuild analysis_last5d/ from logs. Run from Arbiter project root: python build_analysis_last5d.py
from __future__ import annotations

import csv
import re
import shutil
import time
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
OUT = BASE / "analysis_last5d"
LOG_IN = BASE / "logs" / "session.log"
REPORTS = BASE / "Reports"
DATA = BASE / "data" / "daily_metrics_cache.csv"
ET = ZoneInfo("America/New_York")
# US equity RTH in Eastern
RTH_START = dtime(9, 30, 0)
RTH_END = dtime(16, 0, 0)  # inclusive of 16:00:00
LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(?:INFO|WARNING|ERROR|REPORT_MIRROR|CRITICAL)\]"
)

# Downsample: keep every Nth "Skipped:" / "Confirmed:" noise line in RTH to cap size; always keep other levels
SIGNAL_EVERY_N = 8


def _max_trade_date() -> date | None:
    p = BASE / "logs" / "trades.csv"
    if not p.is_file():
        return None
    best: date | None = None
    with open(p, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            d = row.get("Date", "").strip()
            if not d:
                continue
            y, m, d_ = map(int, d.split("-"))
            cur = date(y, m, d_)
            if best is None or cur > best:
                best = cur
    return best


def last_n_weekday_sessions(anchor: date, n: int) -> list[date]:
    """n most recent Mon–Fri dates ending at anchor (NYSE holidays not subtracted)."""
    days: list[date] = []
    d = anchor
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def to_et(naive_local: datetime) -> datetime:
    sec = time.mktime(naive_local.timetuple())
    return datetime.fromtimestamp(sec, tz=ET)


def in_rth_et(et: datetime) -> bool:
    t = et.timetz().replace(tzinfo=None)  # wall time in NY
    if et.weekday() >= 5:
        return False
    if t < RTH_START:
        return False
    if t > RTH_END:
        return False
    return True


def filter_session_log(trading_days: set[date], out_path: Path) -> tuple[int, int, int]:
    """Return (lines_read, lines_written, lines_skipped)."""
    if not LOG_IN.is_file():
        return 0, 0, 0
    read = written = skip = 0
    signal_counter = 0
    with open(LOG_IN, encoding="utf-8", errors="replace") as fin, open(
        out_path, "w", encoding="utf-8", newline=""
    ) as fout:
        fout.write(
            f"# RTH in US/Eastern (09:30–16:00) for sessions {min(trading_days)}..{max(trading_days)}. "
            f"Source: session.log. Timestamps are the PC's local time; the filter uses the same instant in ET. "
            f"Skipped/Confirmed lines: 1 in {SIGNAL_EVERY_N}.\n"
        )
        for line in fin:
            read += 1
            m = LINE_RE.match(line)
            if not m:
                continue
            try:
                naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            et = to_et(naive)
            d_et = et.date()
            if d_et not in trading_days or not in_rth_et(et):
                skip += 1
                continue
            if " Skipped:" in line or " Confirmed:" in line:
                signal_counter += 1
                if signal_counter % SIGNAL_EVERY_N != 0:
                    continue
            fout.write(line)
            written += 1
    return read, written, skip


def filter_csv_by_date(
    src: Path, dest: Path, date_set: set[str], date_column: str = "Date"
) -> None:
    if not src.is_file():
        return
    with open(src, newline="", encoding="utf-8") as fin, open(
        dest, "w", newline="", encoding="utf-8"
    ) as fout:
        r = csv.DictReader(fin)
        if not r.fieldnames:
            return
        w = csv.DictWriter(fout, fieldnames=r.fieldnames, lineterminator="\n")
        w.writeheader()
        for row in r:
            if row.get(date_column, "") in date_set:
                w.writerow(row)


def main() -> None:
    anchor = _max_trade_date()
    if anchor is None:
        raise SystemExit("No dates in logs/trades.csv")
    days = last_n_weekday_sessions(anchor, 5)
    day_strs = {d.isoformat() for d in days}
    trading_set = set(days)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "logs").mkdir(exist_ok=True)
    (OUT / "Reports").mkdir(exist_ok=True)
    (OUT / "data").mkdir(exist_ok=True)

    for name in (
        "trades.csv",
        "trade_outcomes.csv",
        "signals.csv",
        "daily_equity.csv",
        "daily_regime.csv",
    ):
        filter_csv_by_date(
            BASE / "logs" / name, OUT / "logs" / name, day_strs, "Date"
        )
    if DATA.is_file():
        shutil.copy2(DATA, OUT / "data" / "daily_metrics_cache.csv")
    for d in days:
        rep = REPORTS / f"daily_report_{d.isoformat()}.txt"
        if rep.is_file():
            shutil.copy2(rep, OUT / "Reports" / rep.name)

    session_out = OUT / "logs" / "session_rth_excerpt.log"
    r, w, sk = filter_session_log(trading_set, session_out)

    readme = OUT / "README.txt"
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "Arbiter analysis pack: last 5 Mon–Fri session dates ending on the latest Date in "
            "logs/trades.csv (market holidays not removed).\n"
            f"Trading days: {', '.join(d.isoformat() for d in days)}\n\n"
            "session_rth_excerpt.log: only lines whose instant falls in RTH in America/New_York. "
            "Raw timestamps are local PC time, so a line can show e.g. 00:00 when that moment is 10:00 ET.\n\n"
            "Included:\n"
            "  logs/trades.csv, trade_outcomes.csv, signals.csv — rows for those dates only\n"
            "  logs/daily_equity.csv, daily_regime.csv — same\n"
            "  logs/session_rth_excerpt.log — session.log lines in RTH (US/Eastern 09:30–16:00),\n"
            f"     with Skipped/Confirmed lines subsampled 1 in {SIGNAL_EVERY_N} to limit size\n"
            "  Reports/daily_report_*.txt for each day that exists\n"
            "  data/daily_metrics_cache.csv (full snapshot, no date column)\n\n"
            f"Regenerate: python build_analysis_last5d.py\n\n"
            f"session.log: read {r} lines, kept {w} (dropped: not in date set or not RTH in ET, ~{sk}).\n"
        )

    print(f"Created {OUT} — session excerpt: {w} lines (from {r} total).")


if __name__ == "__main__":
    main()
