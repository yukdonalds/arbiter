# -----------------------------
# Fast Paper Trader – External screening (non-IB) for full S&P 500
# Uses Wikipedia for S&P 500 list + yfinance for daily OHLCV. Same filters/score as v26.
# -----------------------------
"""
Screen the entire S&P 500 using free data; no IB historical requests.
Returns (watchlist, daily_metrics) in same format as parallel_fetch.build_watchlist_parallel.
"""
import io
import urllib.request
import warnings
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

import config

def _candidate_score(metrics: dict) -> float:
    prev = metrics.get("prev_close") or 0
    yesterday = metrics.get("yesterday_close") or prev
    if prev <= 0:
        return 0.0
    pct_1d = (yesterday - prev) / prev * 100
    rel_vol = 1.5
    a, b, c = config.SCORE_WEIGHTS
    return pct_1d * a + (rel_vol - 1.0) * b + (metrics.get("atr_pct") or 0) * c


def _get_sp500_symbols() -> list[str]:
    """S&P 500 constituent symbols from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    # Wikipedia returns 403 without a browser-like User-Agent (pd.read_html has no header support)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode()
    # Pass as file-like so parser never treats the string as a path (avoids [Errno 2] on Windows)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tables = pd.read_html(io.BytesIO(html.encode("utf-8")))
    # First table may be nav/TOC; find the one with Symbol/Ticker (constituents table)
    df = None
    for t in tables:
        if "Symbol" in t.columns or "Ticker" in t.columns:
            if len(t) >= 400:  # S&P 500 has ~500 rows
                df = t
                break
    if df is None:
        df = tables[0]  # fallback
    col = "Symbol" if "Symbol" in df.columns else "Ticker"
    symbols = df[col].astype(str).str.strip().dropna().unique().tolist()
    return [s.upper() for s in symbols if s and s != "NAN"]


def _metrics_from_df(df: pd.DataFrame) -> dict | None:
    """Compute avg_vol_20, atr_pct, prev_close, yesterday_close from daily OHLCV DataFrame."""
    if df is None or len(df) < config.ATR_PERIOD + 1:
        return None
    for c in ("Close", "High", "Low", "Volume"):
        if c not in df.columns:
            return None
    vol = df["Volume"].astype(float)
    avg_vol = vol.rolling(config.VOLUME_LOOKBACK, min_periods=1).mean().iloc[-1]
    yesterday_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else yesterday_close
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    tr_list = []
    for j in range(1, min(config.ATR_PERIOD + 1, len(closes))):
        h, l_, c_ = highs[-j], lows[-j], closes[-j]
        prev_c = closes[-j - 1] if len(closes) > j else c_
        tr = max(h - l_, abs(h - prev_c), abs(l_ - prev_c))
        tr_list.append(tr)
    atr = sum(tr_list) / len(tr_list) if tr_list else 0.0
    atr_pct = (atr / closes[-1] * 100) if closes[-1] else 0.0
    return {
        "avg_vol_20": avg_vol,
        "atr_pct": atr_pct,
        "prev_close": prev_close,
        "yesterday_close": yesterday_close,
        "today_volume_so_far": 0.0,
    }


def _tickers_from_multiindex(data: pd.DataFrame) -> list:
    if not isinstance(data.columns, pd.MultiIndex):
        return []
    lev0 = data.columns.get_level_values(0).unique().tolist()
    lev1 = data.columns.get_level_values(1).unique().tolist()
    if lev0 and all(isinstance(x, str) and len(x) <= 6 for x in lev0[:5]):
        return lev0
    return lev1


def build_watchlist_external(top_n: int = 100) -> tuple[list[str], dict] | None:
    """Screen full S&P 500 using Wikipedia + yfinance. No IB. Returns (watchlist, daily_metrics) or None."""
    try:
        symbols = _get_sp500_symbols()
    except Exception as e:
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:200] + "..."
        print(f"  External screen: failed to get S&P 500 list: {msg}")
        return None
    if not symbols:
        print("  External screen: no symbols from Wikipedia")
        return None
    print(f"  External screen: got {len(symbols)} S&P 500 symbols, fetching daily data (yfinance)...")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = yf.download(
            symbols,
            period="1mo",
            interval="1d",
            group_by="ticker",
            threads=True,
            auto_adjust=True,
            progress=False,
        )

    if data.empty:
        print("  External screen: no data returned from yfinance")
        return None

    metrics = {}
    if isinstance(data.columns, pd.MultiIndex):
        tickers_in_data = _tickers_from_multiindex(data)
        for sym in tickers_in_data:
            try:
                sub = data[sym].copy()
                if sub is None or sub.empty or len(sub) < 2:
                    continue
                sub = sub.dropna(subset=["Close", "High", "Low", "Volume"])
                if len(sub) < config.ATR_PERIOD + 1:
                    continue
                m = _metrics_from_df(sub)
                if m is None:
                    continue
                if not (config.MIN_AVG_DAILY_VOLUME <= m["avg_vol_20"]):
                    continue
                if not (config.PRICE_MIN <= m["yesterday_close"] <= config.PRICE_MAX):
                    continue
                if not (config.ATR_PCT_MIN <= m["atr_pct"] <= config.ATR_PCT_MAX):
                    continue
                metrics[sym] = m
            except Exception:
                continue
    else:
        sym = symbols[0] if symbols else "UNKNOWN"
        sub = data.dropna(subset=["Close", "High", "Low", "Volume"])
        if len(sub) >= config.ATR_PERIOD + 1:
            m = _metrics_from_df(sub)
            if m and config.MIN_AVG_DAILY_VOLUME <= m["avg_vol_20"] and config.PRICE_MIN <= m["yesterday_close"] <= config.PRICE_MAX and config.ATR_PCT_MIN <= m["atr_pct"] <= config.ATR_PCT_MAX:
                metrics[sym] = m

    if not metrics:
        print("  External screen: no tickers passed filters")
        return None

    scored = [(sym, _candidate_score(m)) for sym, m in metrics.items()]
    scored.sort(key=lambda x: (x[1], x[0]), reverse=True)
    watchlist = [s for s, _ in scored[:top_n]]
    watch_metrics = {s: metrics[s] for s in watchlist if s in metrics}
    print(f"  External screen: {len(metrics)} passed filters, top {len(watchlist)}; top 5: {', '.join(watchlist[:5])}")
    return watchlist, watch_metrics


def build_watchlist_external_for_date(
    prior_trading_date: date,
    top_n: int = 100,
    use_backtest_volume_min: bool = False,
) -> tuple[list[str], dict] | None:
    """
    Screen S&P 500 as of a specific prior trading date (for backtest rescan).
    Fetches daily OHLCV ending on prior_trading_date, computes metrics, filters, ranks.
    Returns (watchlist, daily_metrics) or None on failure.
    """
    min_vol = getattr(config, "BACKTEST_MIN_AVG_DAILY_VOLUME", 300_000) if use_backtest_volume_min else config.MIN_AVG_DAILY_VOLUME
    try:
        symbols = _get_sp500_symbols()
    except Exception as e:
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:200] + "..."
        print(f"  External screen (date={prior_trading_date}): failed to get S&P 500 list: {msg}")
        return None
    if not symbols:
        print(f"  External screen (date={prior_trading_date}): no symbols")
        return None

    start_d = prior_trading_date - timedelta(days=95)
    end_d = prior_trading_date + timedelta(days=1)
    start_str = start_d.strftime("%Y-%m-%d")
    end_str = end_d.strftime("%Y-%m-%d")

    print(f"  yfinance: downloading S&P 500 daily data ({len(symbols)} tickers)...", flush=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = yf.download(
            symbols,
            start=start_str,
            end=end_str,
            interval="1d",
            group_by="ticker",
            threads=True,
            auto_adjust=True,
            progress=True,
        )

    if data.empty:
        print(f"  External screen (date={prior_trading_date}): no yfinance data")
        return None

    metrics = {}
    if isinstance(data.columns, pd.MultiIndex):
        tickers_in_data = _tickers_from_multiindex(data)
        for sym in tickers_in_data:
            try:
                sub = data[sym].copy()
                if sub is None or sub.empty or len(sub) < 2:
                    continue
                sub = sub.dropna(subset=["Close", "High", "Low", "Volume"])
                if len(sub) < config.ATR_PERIOD + 1:
                    continue
                m = _metrics_from_df(sub)
                if m is None:
                    continue
                if not (min_vol <= m["avg_vol_20"]):
                    continue
                if not (config.PRICE_MIN <= m["yesterday_close"] <= config.PRICE_MAX):
                    continue
                if not (config.ATR_PCT_MIN <= m["atr_pct"] <= config.ATR_PCT_MAX):
                    continue
                metrics[sym] = m
            except Exception:
                continue
    else:
        sym = symbols[0] if symbols else "UNKNOWN"
        sub = data.dropna(subset=["Close", "High", "Low", "Volume"])
        if len(sub) >= config.ATR_PERIOD + 1:
            m = _metrics_from_df(sub)
            if m and min_vol <= m["avg_vol_20"] and config.PRICE_MIN <= m["yesterday_close"] <= config.PRICE_MAX and config.ATR_PCT_MIN <= m["atr_pct"] <= config.ATR_PCT_MAX:
                metrics[sym] = m

    if not metrics:
        print(f"  External screen (date={prior_trading_date}): no tickers passed filters")
        return None

    scored = [(sym, _candidate_score(m)) for sym, m in metrics.items()]
    scored.sort(key=lambda x: (x[1], x[0]), reverse=True)
    watchlist = [s for s, _ in scored[:top_n]]
    watch_metrics = {s: metrics[s] for s in watchlist if s in metrics}
    return watchlist, watch_metrics
