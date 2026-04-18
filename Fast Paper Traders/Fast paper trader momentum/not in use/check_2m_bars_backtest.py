# -----------------------------
# Back-check LYB, HST, MRNA for 2026-03-04 using 2-minute bars (yfinance).
# Runs v26 check on each 2m bar to see if any would have qualified.
# -----------------------------
import sys
import os
import warnings
from datetime import datetime

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from signal_engine import check_v26_bar

BAR_MINUTES = getattr(config, "BAR_MINUTES", 2)
TICKERS = ["LYB", "HST", "MRNA"]
TARGET_DATE = "2026-03-04"


def get_daily_metrics(sym: str, target_date: pd.Timestamp) -> dict | None:
    """Prev close, avg_vol_20, atr_pct for target_date (using data up to that day)."""
    start = target_date - pd.Timedelta(days=60)
    end = target_date + pd.Timedelta(days=2)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.download(sym, start=start, end=end, interval="1d", auto_adjust=True, progress=False, threads=False, group_by="ticker")
    if df is None or df.empty or len(df) < config.ATR_PERIOD + 2:
        return None
    # Normalize columns (yfinance single-ticker can be MultiIndex or flat; ensure we have Open,High,Low,Close,Volume)
    if isinstance(df.columns, pd.MultiIndex):
        df = df[sym].copy() if sym in df.columns.get_level_values(0) else df.iloc[:, :5].copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(1)
        df = pd.DataFrame(df)
    col_map = {}
    for c in df.columns:
        cstr = str(c).strip()
        if cstr.lower() == "open": col_map[c] = "Open"
        elif cstr.lower() == "high": col_map[c] = "High"
        elif cstr.lower() == "low": col_map[c] = "Low"
        elif cstr.lower() == "close": col_map[c] = "Close"
        elif cstr.lower() == "volume": col_map[c] = "Volume"
    df = df.rename(columns=col_map)
    need = ["Close", "High", "Low", "Volume"]
    if not all(c in df.columns for c in need):
        return None
    df = df.dropna(subset=need)
    if len(df) < config.VOLUME_LOOKBACK + 2:
        return None
    df = df.sort_index()
    # Use last available date on or before target (index may be tz-aware)
    try:
        dates = df.index.normalize() if hasattr(df.index, "normalize") else df.index
    except Exception:
        dates = df.index
    mask = dates <= pd.Timestamp(target_date)
    if not mask.any():
        return None
    idx = df.index[mask][-1]
    i = df.index.get_loc(idx)
    if i < config.ATR_PERIOD or i < 1:
        return None
    prev_close = float(df["Close"].iloc[i - 1])
    vol = df["Volume"].iloc[: i].astype(float)
    avg_vol = vol.rolling(config.VOLUME_LOOKBACK, min_periods=1).mean().iloc[-1]
    highs = df["High"].values[: i + 1]
    lows = df["Low"].values[: i + 1]
    closes = df["Close"].values[: i + 1]
    tr_list = []
    for j in range(1, min(config.ATR_PERIOD + 1, len(closes))):
        h, l_, c_ = highs[-j], lows[-j], closes[-j]
        prev_c = closes[-j - 1] if len(closes) > j else c_
        tr_list.append(max(h - l_, abs(h - prev_c), abs(l_ - prev_c)))
    atr = sum(tr_list) / len(tr_list) if tr_list else 0.0
    atr_pct = (atr / closes[-1] * 100) if closes[-1] else 0.0
    return {
        "prev_close": prev_close,
        "avg_vol_20": avg_vol,
        "atr_pct": atr_pct,
        "today_volume_so_far": 0.0,
    }


def fetch_2m_bars(sym: str, target_date: pd.Timestamp) -> list[dict]:
    """Fetch intraday and return list of 2m bars for target_date. Each bar: open, high, low, close, volume."""
    end = target_date + pd.Timedelta(days=2)
    start = target_date - pd.Timedelta(days=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # Try 2m first; yfinance may only have 1m for some ranges
        df = yf.download(sym, start=start, end=end, interval="2m", auto_adjust=True, progress=False, threads=False)
    if df is None or df.empty:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.download(sym, start=start, end=end, interval="1m", auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            return []
        # Aggregate 1m -> 2m
        df = df[df.index.date == target_date.date()]
        if df.empty:
            return []
        df = df.sort_index()
        df["bar_time"] = df.index.floor("2min")
        agg = df.groupby("bar_time").agg(
            open=("Open", "first"),
            high=("High", "max"),
            low=("Low", "min"),
            close=("Close", "last"),
            volume=("Volume", "sum"),
        ).reset_index()
        bars = []
        for _, row in agg.iterrows():
            bars.append({
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
        return bars
    df = df[df.index.date == target_date.date()]
    if df.empty:
        return []
    df = df.sort_index()
    # Flatten MultiIndex columns for single-ticker
    if isinstance(df.columns, pd.MultiIndex):
        if sym in df.columns.get_level_values(0):
            df = df[sym].copy()
        else:
            df.columns = df.columns.get_level_values(1)
    bars = []
    for _, row in df.iterrows():
        o = row["Open"] if "Open" in row.index else row.iloc[0]
        h = row["High"] if "High" in row.index else row.iloc[1]
        l_ = row["Low"] if "Low" in row.index else row.iloc[2]
        c = row["Close"] if "Close" in row.index else row.iloc[3]
        v = row["Volume"] if "Volume" in row.index else (row.iloc[4] if len(row) > 4 else 0)
        bars.append({
            "open": float(o),
            "high": float(h),
            "low": float(l_),
            "close": float(c),
            "volume": float(v),
        })
    return bars


def main():
    target_date = pd.Timestamp(TARGET_DATE)
    if target_date.weekday() >= 5:
        target_date = target_date - pd.Timedelta(days=target_date.weekday() - 4)

    print(f"  Back-checking 2m bars for {TICKERS} on {target_date.date()} (v26 criteria)\n")

    for sym in TICKERS:
        print(f"  --- {sym} ---")
        dm = get_daily_metrics(sym, target_date)
        if not dm:
            print(f"    No daily metrics.")
            continue
        if dm["avg_vol_20"] < config.MIN_AVG_DAILY_VOLUME:
            print(f"    avg_vol_20 {dm['avg_vol_20']:.0f} < MIN_AVG_DAILY_VOLUME")
            continue
        bars_2m = fetch_2m_bars(sym, target_date)
        if not bars_2m:
            print(f"    No 2m intraday data from yfinance for this date.")
            continue
        print(f"    prev_close={dm['prev_close']:.2f} avg_vol_20={dm['avg_vol_20']:.0f} atr_pct={dm['atr_pct']:.2f}%")
        print(f"    {len(bars_2m)} two-minute bars.")

        today_vol_so_far = 0.0
        passed = []
        for i, bar in enumerate(bars_2m):
            dm["today_volume_so_far"] = today_vol_so_far
            eligible, score = check_v26_bar(sym, bar, dm)
            today_vol_so_far += bar.get("volume", 0)
            if eligible:
                passed.append((i, bar, score))
        if passed:
            print(f"    PASSED v26 on {len(passed)} bar(s):")
            for idx, bar, score in passed[:10]:
                print(f"      bar {idx}: close={bar['close']:.2f} vol={bar.get('volume',0):.0f} score={score:.2f}")
            if len(passed) > 10:
                print(f"      ... and {len(passed) - 10} more")
        else:
            print(f"    No bar passed v26.")
        print()

    print("  Done. (If no 2m data: yfinance may not have intraday for this date.)")


if __name__ == "__main__":
    main()

