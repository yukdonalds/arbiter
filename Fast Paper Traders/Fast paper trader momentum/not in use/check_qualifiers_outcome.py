# -----------------------------
# Check qualifiers for the last trading day: would long at that day's open hit +4% or -3%?
# Uses that same day's OHLC: entry = Open, then High/Low for TP vs STP.
# Usage: python check_qualifiers_outcome.py [YYYY-MM-DD]
#   Default: last session date from Reports/ (or 2026-03-16). Qualifiers from check_qualifiers_daily.py.
# -----------------------------
import sys
import os
import warnings

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

TARGET_PCT = getattr(config, "TARGET_PCT", 0.04)
STOP_PCT = getattr(config, "STOP_PCT", 0.03)


def outcome_first_touch(entry: float, target_pct: float, stop_pct: float, high: float, low: float) -> str:
    """Return 'TP' if target hit first, 'STP' if stop hit first, 'BOTH' if both hit, 'NONE' if neither."""
    target = entry * (1 + target_pct)
    stop = entry * (1 - stop_pct)
    hit_tp = high >= target
    hit_stp = low <= stop
    if hit_tp and not hit_stp:
        return "TP"
    if hit_stp and not hit_tp:
        return "STP"
    if hit_tp and hit_stp:
        return "BOTH"  # both levels hit same day; order unknown
    return "NONE"


def get_last_session_date():
    """Return YYYY-MM-DD of the most recent daily report, or None."""
    report_dir = getattr(config, "REPORT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "Reports"))
    if not os.path.isdir(report_dir):
        return None
    import re
    pattern = re.compile(r"daily_report_(\d{4}-\d{2}-\d{2})\.txt$")
    best_date = None
    best_mtime = 0
    for name in os.listdir(report_dir):
        m = pattern.match(name)
        if m:
            path = os.path.join(report_dir, name)
            try:
                mtime = os.path.getmtime(path)
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_date = m.group(1)
            except OSError:
                pass
    return best_date


def main():
    # Date: argv, or last session from Reports/, or 2026-03-16
    target_date_str = sys.argv[1] if len(sys.argv) > 1 else (get_last_session_date() or "2026-03-16")
    target_date = pd.Timestamp(target_date_str)
    # 2026-03-16 qualifiers (ticker, close from check_qualifiers_daily); we use that day's Open as entry
    qualifiers = [
        ("DLTR", 114.36),
        ("SMCI", 31.86),
        ("CRH", 103.02),
        ("MDLZ", 57.16),
        ("ALGN", 169.45),
        ("LULU", 159.91),
        ("APTV", 71.57),
        ("HAL", 34.16),
    ]
    print(f"  Last trading day: {target_date.date()}")
    print(f"  Qualifiers: would long at that day's OPEN have hit TP (+{TARGET_PCT*100:.0f}%) or STP (-{STOP_PCT*100:.0f}%)?")
    print(f"  (Using same day High/Low.)\n")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for ticker, _ in qualifiers:
            try:
                df = yf.download(
                    ticker, start=target_date - pd.Timedelta(days=5), end=target_date + pd.Timedelta(days=2),
                    interval="1d", auto_adjust=True, progress=False, threads=False, group_by="ticker"
                )
            except Exception:
                print(f"    {ticker}: no data")
                continue
            if df is None or df.empty:
                print(f"    {ticker}: no data")
                continue
            if isinstance(df.columns, pd.MultiIndex) and ticker in df.columns.get_level_values(0):
                df = df[ticker].copy()
            df = pd.DataFrame(df)
            df.columns = [str(c).strip() for c in df.columns]
            need = ["Open", "High", "Low", "Close"]
            if not all(c in df.columns for c in need):
                print(f"    {ticker}: missing OHLC")
                continue
            # Row on target_date (last trading day)
            try:
                idx_norm = df.index.normalize() if hasattr(df.index, "normalize") else pd.DatetimeIndex(df.index).normalize()
            except Exception:
                idx_norm = df.index
            mask = idx_norm <= pd.Timestamp(target_date).normalize()
            if not mask.any():
                print(f"    {ticker}: no data on or before {target_date.date()}")
                continue
            row = df.loc[mask].iloc[-1]
            open_ = float(row["Open"])
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
            # Entry = open of that day; did we hit target or stop during the day?
            entry = open_
            res = outcome_first_touch(entry, TARGET_PCT, STOP_PCT, high, low)
            target_px = entry * (1 + TARGET_PCT)
            stop_px = entry * (1 - STOP_PCT)
            if res == "TP":
                pct = (target_px / entry - 1) * 100
                print(f"    {ticker}: TP  (open={entry:.2f} high={high:.2f} >= target {target_px:.2f})  +{pct:.1f}%")
            elif res == "STP":
                pct = (stop_px / entry - 1) * 100
                print(f"    {ticker}: STP (open={entry:.2f} low={low:.2f} <= stop {stop_px:.2f})  {pct:.1f}%")
            elif res == "BOTH":
                print(f"    {ticker}: BOTH (open={entry:.2f} high={high:.2f} low={low:.2f}; target {target_px:.2f} stop {stop_px:.2f})")
            else:
                print(f"    {ticker}: NONE (open={entry:.2f} high={high:.2f} low={low:.2f}; target {target_px:.2f} stop {stop_px:.2f})")
    print("\n  Note: Entry = open of last trading day; real app uses 2m bars and fills during the day.")


if __name__ == "__main__":
    main()

