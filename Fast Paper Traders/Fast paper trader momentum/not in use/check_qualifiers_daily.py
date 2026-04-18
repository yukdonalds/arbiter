# -----------------------------
# One-off: Daily scan for a given date using same v26 logic as the app.
# Usage: python check_qualifiers_daily.py [YYYY-MM-DD]
#        python check_qualifiers_daily.py              # uses last session date from Reports/
#        python check_qualifiers_daily.py --ask         # prompt for date
# -----------------------------
"""
Check which S&P 500 tickers would have qualified for v26 on a given day
using the app's check_v26_bar() and config. Daily OHLCV is used as one "bar"
(approximation; real app uses 2-min bars).
"""
import sys
import re
import warnings
import os

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from signal_engine import check_v26_bar


def get_last_session_date():
    """Return YYYY-MM-DD of the most recent daily report, or None."""
    report_dir = getattr(config, "REPORT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "Reports"))
    if not os.path.isdir(report_dir):
        return None
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


def load_tickers():
    out = []
    path = config.SP500_TICKERS_FILE
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.upper())
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().upper() in ("--ASK", "-A"):
        date_str = input("Enter date to check (YYYY-MM-DD): ").strip() or None
        if not date_str:
            print("  No date entered. Exiting.")
            return
    elif len(sys.argv) > 1:
        date_str = sys.argv[1].strip()
    else:
        date_str = get_last_session_date()
        if date_str:
            print(f"  Using last session date from Reports: {date_str}")
        else:
            date_str = input("Enter date to check (YYYY-MM-DD): ").strip()
            if not date_str:
                print("  No date entered. Exiting.")
                return
    target_date = pd.Timestamp(date_str)
    if target_date.weekday() >= 5:
        target_date = target_date - pd.Timedelta(days=target_date.weekday() - 4)

    tickers = load_tickers()
    if not tickers:
        print("  No tickers in sp500_tickers.txt.")
        return

    print(f"  Checking v26 daily qualifiers for {target_date.date()} ({len(tickers)} tickers)...")
    end = target_date + pd.Timedelta(days=5)
    start = target_date - pd.Timedelta(days=60)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = yf.download(
            tickers,
            start=start,
            end=end,
            interval="1d",
            group_by="ticker",
            threads=True,
            auto_adjust=True,
            progress=False,
        )

    if data.empty:
        print("  No data from yfinance.")
        return

    if not isinstance(data.columns, pd.MultiIndex):
        print("  Single-ticker response.")
        return

    qualifiers = []
    row_date = target_date
    for sym in data.columns.get_level_values(0).unique().tolist():
        try:
            sub = data[sym].copy()
            if sub is None or sub.empty or len(sub) < config.ATR_PERIOD + 2:
                continue
            sub = sub.dropna(subset=["Close", "High", "Low", "Volume"])
            if len(sub) < config.VOLUME_LOOKBACK + 2:
                continue
            sub = sub.loc[~sub.index.duplicated(keep="first")]
            sub = sub.sort_index()

            dates = sub.index
            mask = dates <= target_date
            if not mask.any():
                continue
            row_date = dates[mask][-1]
            idx = sub.index.get_loc(row_date)
            if idx < config.ATR_PERIOD or idx < 1:
                continue

            row = sub.iloc[idx]
            prev_close = float(sub["Close"].iloc[idx - 1])
            close = float(row["Close"])
            high = float(row["High"])
            low = float(row["Low"])
            vol_today = float(row["Volume"])
            open_ = float(row["Open"]) if "Open" in row else close

            vol_series = sub["Volume"].iloc[:idx].astype(float)
            avg_vol = vol_series.rolling(config.VOLUME_LOOKBACK, min_periods=1).mean().iloc[-1]

            highs = sub["High"].values[: idx + 1]
            lows = sub["Low"].values[: idx + 1]
            closes = sub["Close"].values[: idx + 1]
            tr_list = []
            for j in range(1, min(config.ATR_PERIOD + 1, len(closes))):
                h, l_, c_ = highs[-j], lows[-j], closes[-j]
                prev_c = closes[-j - 1] if len(closes) > j else c_
                tr = max(h - l_, abs(h - prev_c), abs(l_ - prev_c))
                tr_list.append(tr)
            atr = sum(tr_list) / len(tr_list) if tr_list else 0.0
            atr_pct = (atr / close * 100) if close else 0.0

            daily_metrics = {
                "avg_vol_20": avg_vol,
                "atr_pct": atr_pct,
                "prev_close": prev_close,
                "today_volume_so_far": 0.0,
            }
            bar = {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": vol_today,
            }
            eligible, score = check_v26_bar(sym, bar, daily_metrics)
            if not eligible:
                continue

            pct_change_1d = (close - prev_close) / prev_close * 100 if prev_close else 0.0
            rel_vol = (vol_today / avg_vol) if avg_vol else 0.0
            vwap = (high + low + close) / 3.0
            dist_vwap = (close - vwap) / vwap * 100 if vwap else 0.0
            qualifiers.append({
                "ticker": sym,
                "close": close,
                "score": score,
                "pct_change_1d": round(pct_change_1d, 2),
                "rel_vol": round(rel_vol, 2),
                "atr_pct": round(atr_pct, 2),
                "dist_vwap_pct": round(dist_vwap, 2),
            })
        except Exception:
            continue

    qualifiers.sort(key=lambda x: x["score"], reverse=True)
    print(f"  Date used: {row_date}")
    print(f"  Tickers passing v26 (same as app): {len(qualifiers)}")
    if qualifiers:
        for q in qualifiers[:30]:
            print(f"    {q['ticker']}: close={q['close']:.2f} score={q['score']:.2f} pct_1d={q['pct_change_1d']}% rel_vol={q['rel_vol']} atr_pct={q['atr_pct']}% dist_vwap={q['dist_vwap_pct']}%")
        if len(qualifiers) > 30:
            print(f"    ... and {len(qualifiers) - 30} more")
    else:
        print("  None (no tickers met app v26 filters on this day at daily level).")
    print("\n  Note: App uses 2-min bars; this uses one daily bar with same check_v26_bar() and config.")


if __name__ == "__main__":
    main()

