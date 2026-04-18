"""One-off: daily range stats for watchlist vs SPY on a given date."""
import sys

import pandas as pd
import yfinance as yf

DAY = "2026-04-17"
START, END = "2026-04-15", "2026-04-21"


def day_row(df, d):
    dt = pd.Timestamp(d).date()
    for i in range(len(df)):
        if pd.Timestamp(df.index[i]).date() == dt:
            return df.iloc[i]
    return None


def normalize_cols(df):
    if getattr(df.columns, "nlevels", 1) > 1:
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def main():
    with open("data/watchlist_cache.txt", encoding="utf-8") as f:
        tickers = [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]

    spy_df = normalize_cols(yf.download("SPY", start=START, end=END, progress=False, auto_adjust=True))
    rs = day_row(spy_df, DAY)
    spy_rng = (float(rs["High"]) - float(rs["Low"])) / float(rs["Open"]) * 100 if rs is not None else 0
    spy_ret = (float(rs["Close"]) - float(rs["Open"])) / float(rs["Open"]) * 100 if rs is not None else 0
    print(f"Benchmark SPY {DAY}: H-L range % of open = {spy_rng:.2f}%, open-to-close = {spy_ret:.2f}%")
    print()

    rows = []
    failed = []
    chunk = 30
    for i in range(0, len(tickers), chunk):
        batch = tickers[i : i + chunk]
        try:
            data = yf.download(batch, start=START, end=END, progress=False, auto_adjust=True, threads=True)
        except Exception:
            failed.extend(batch)
            continue
        if len(batch) == 1:
            data = normalize_cols(data)
            sym = batch[0]
            r = day_row(data, DAY)
            if r is not None:
                o, h, l, c = float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])
                if o:
                    rng = (h - l) / o * 100
                    o2c = (c - o) / o * 100
                    rows.append((sym, o2c, rng, abs(o2c)))
            continue
        if getattr(data.columns, "nlevels", 1) <= 1:
            failed.extend(batch)
            continue
        for sym in data.columns.get_level_values(1).unique():
            try:
                dsub = data.xs(sym, axis=1, level=1)
                dsub = normalize_cols(dsub)
                r = day_row(dsub, DAY)
                if r is None:
                    continue
                o, h, l, c = float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"])
                if not o:
                    continue
                rng = (h - l) / o * 100
                o2c = (c - o) / o * 100
                rows.append((sym, o2c, rng, abs(o2c)))
            except Exception:
                failed.append(sym)

    df = pd.DataFrame(rows, columns=["sym", "o2c_pct", "hl_range_pct", "abs_o2c"])
    df = df.sort_values("hl_range_pct", ascending=False)
    print(f"Top 25 by intraday H-L range % (of open), {DAY}")
    print(df.head(25).to_string(index=False))
    print()
    print(f"Summary for {len(rows)} symbols with data (of {len(tickers)} in file)")
    for thr in [1.0, 1.5, 2.0, 3.0]:
        n = int((df["hl_range_pct"] >= thr).sum())
        print(f"  H-L range >= {thr}%: {n} tickers")
    thr2 = max(1.5, spy_rng * 1.2)
    high_action = df[df["hl_range_pct"] >= thr2]
    print()
    print(f'Symbols with H-L range >= max(1.5%, SPY_range*1.2={thr2:.2f}%): {len(high_action)} tickers')
    if len(failed):
        u = list(dict.fromkeys(failed))[:20]
        print("Parse/fetch issues (sample):", ", ".join(u))


if __name__ == "__main__":
    main()
