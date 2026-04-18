# -----------------------------
# v2.6 S&P 500 Validation – Multi-Year Database (IB data, same as main)
# -----------------------------
# Asks which year to run. Loads tickers from local file (same as main);
# fetches daily OHLCV from IB (TWS), applies v2.6 rules. Saves to data/YYYY/.
# Requires: TWS/IB Gateway running, API enabled (port 7497 paper). pip install ib_insync
# -----------------------------

import os
import time
import random
import pandas as pd
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# IB connection (same as main: TWS paper)
IB_HOST = "127.0.0.1"
IB_PORT = 7497
SLEEP_BETWEEN_TICKERS = 0.6  # avoid rate limits
HIST_TIMEOUT = 30

try:
    from ib_insync import IB, Stock, util
except ImportError:
    print("Need: pip install ib_insync")
    raise SystemExit(1)

def make_stock(symbol: str) -> Stock:
    symbol = symbol.replace(".", "-")
    return Stock(symbol, "SMART", "USD")


# -----------------------------
# 1. Ask which year to run
# -----------------------------
def get_year():
    while True:
        raw = input("What year to run? (e.g. 2022): ").strip()
        if not raw:
            raw = "2022"
        try:
            y = int(raw)
            if 1990 <= y <= 2030:
                return y
        except ValueError:
            pass
        print("  Please enter a valid year (e.g. 2022).")

YEAR = get_year()
start_date = f"{YEAR}-01-01"
end_date = f"{YEAR}-12-31"
year_dir = os.path.join(DATA_DIR, str(YEAR))
os.makedirs(year_dir, exist_ok=True)

print(f"Date range: {start_date} to {end_date} (all of {YEAR})")
print(f"Output folder: {year_dir}\n")

# -----------------------------
# 2. Load tickers (same source as main: local file, then optional scrape fallback)
# -----------------------------
def load_tickers_from_file():
    # Same as main: local sp500_tickers.txt; then try Paper/mission copy
    candidates = [
        os.path.join(BASE_DIR, "sp500_tickers.txt"),
        os.path.join(os.path.dirname(BASE_DIR), "Paper", "mission", "sp500_tickers.txt"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            tickers = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        tickers.append(line.upper())
            if tickers:
                return tickers, path
    return None, None

def load_tickers_from_web():
    import requests
    from bs4 import BeautifulSoup
    url = "https://stockanalysis.com/list/sp-500-stocks/"
    page = requests.get(url, timeout=15).text
    soup = BeautifulSoup(page, "html.parser")
    tickers = []
    table = soup.find("table")
    if table:
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) >= 2:
                t = cols[1].text.strip()
                if t and not t.isdigit():
                    tickers.append(t.upper())
    return tickers

tickers, ticker_path = load_tickers_from_file()
if not tickers:
    print("No sp500_tickers.txt found (copy from Paper/mission for same as main). Using web scrape.")
    tickers = load_tickers_from_web()
    if not tickers:
        print("Could not load tickers. Exiting.")
        raise SystemExit(1)
else:
    print(f"Loaded {len(tickers)} tickers from {ticker_path}")
if not tickers:
    raise SystemExit(1)
print(f"Tickers: {len(tickers)}")

# -----------------------------
# 3. Fetch daily OHLCV from IB (same data source as main)
# -----------------------------
def fetch_daily_ib(ib: IB, symbol: str, end_dt: str):
    try:
        c = make_stock(symbol)
        bars = ib.reqHistoricalData(
            c, end_dt, "1 Y", "1 day", "TRADES", useRTH=True, timeout=HIST_TIMEOUT
        )
        if not bars or len(bars) < 2:
            return None
        df = util.df(bars)
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        # Use bar date as index so v26_rules gets real dates (ticker_data.index[i])
        if "date" in df.columns:
            df = df.set_index(pd.to_datetime(df["date"]))
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        return df
    except Exception:
        return None

# End of year, 16:00 Eastern (after close)
try:
    import pytz
    eastern = pytz.timezone("America/New_York")
    end_dt = datetime(YEAR, 12, 31, 16, 0, 0, tzinfo=eastern)
    end_dt_str = end_dt.strftime("%Y%m%d %H:%M:%S US/Eastern")
except Exception:
    end_dt_str = f"{YEAR}1231 16:00:00 US/Eastern"

print("Connecting to TWS for daily data (same as main)...")
ib = IB()
client_id = random.randint(1, 32)
try:
    ib.connect(IB_HOST, IB_PORT, clientId=client_id, timeout=10)
except Exception as e:
    print(f"Cannot connect to TWS at {IB_HOST}:{IB_PORT}: {e}")
    print("Start TWS/IB Gateway with API enabled (paper 7497).")
    raise SystemExit(1)
print("Connected. Fetching daily bars (this may take a while)...", flush=True)

data_frames = {}
for i, symbol in enumerate(tickers):
    df = fetch_daily_ib(ib, symbol, end_dt_str)
    if df is not None and len(df) >= 15:
        data_frames[symbol] = df
    if (i + 1) % 2 == 0 or i == 0:
        print(f"  Fetched {i + 1}/{len(tickers)} tickers...", flush=True)
    time.sleep(SLEEP_BETWEEN_TICKERS)

try:
    ib.disconnect()
except Exception:
    pass

if not data_frames:
    print("No daily data from IB. Check TWS and market data subscriptions.")
    raise SystemExit(1)

# Build same structure as yf.download(..., group_by='ticker'): MultiIndex columns
data = pd.concat(data_frames, axis=1)
print(f"Fetched {len(data_frames)} tickers from IB.\n")

# -----------------------------
# 4. v2.6 rules (same as main, no look-ahead)
# -----------------------------
def _atr_pct(bars, period=14):
    if len(bars) < period + 1:
        return 0.0
    tr_list = []
    for j in range(1, min(len(bars), period + 1)):
        b, prev = bars.iloc[-j], bars.iloc[-j - 1]
        h, l_ = b["High"], b["Low"]
        prev_c = prev["Close"]
        tr = max(h - l_, abs(h - prev_c), abs(l_ - prev_c))
        tr_list.append(tr)
    atr = sum(tr_list) / len(tr_list) if tr_list else 0.0
    close = bars.iloc[-1]["Close"] or 1.0
    return (atr / close * 100) if close else 0.0


def v26_rules(df, tickers):
    results = []
    MIN_AVG_DAILY_VOLUME = 1_000_000
    VOLUME_LOOKBACK = 20
    PRICE_MIN, PRICE_MAX = 5.0, 150.0
    MIN_PCT_CHANGE_1D = 2.0
    MIN_RELATIVE_VOLUME = 1.5
    ATR_PERIOD = 14
    MIN_ATR_PCT = 1.5
    ATR_PCT_MIN, ATR_PCT_MAX = 1.5, 6.0
    MAX_DISTANCE_FROM_VWAP_PCT = 3.0

    for ticker in tickers:
        if ticker not in df:
            continue
        ticker_data = df[ticker].dropna()
        if len(ticker_data) < ATR_PERIOD + 1:
            continue
        vol = ticker_data["Volume"].astype(float)
        ticker_data = ticker_data.assign(avg_vol_20=vol.shift(1).rolling(VOLUME_LOOKBACK, min_periods=1).mean())
        for i in range(ATR_PERIOD + 1, len(ticker_data)):
            row = ticker_data.iloc[i]
            date = ticker_data.index[i]
            close = float(row["Close"])
            if close <= 0:
                continue
            avg_vol = float(row["avg_vol_20"])
            liquidity_ok = avg_vol >= MIN_AVG_DAILY_VOLUME
            price_ok = PRICE_MIN <= close <= PRICE_MAX
            prev_close = float(ticker_data.iloc[i - 1]["Close"])
            pct_change_1d = (close - prev_close) / prev_close * 100 if prev_close else 0.0
            today_vol = float(row["Volume"])
            rel_vol = (today_vol / avg_vol) if avg_vol else 0.0
            bars_slice = ticker_data.iloc[:i]
            atr_pct = _atr_pct(bars_slice, ATR_PERIOD)
            momentum_ok = (
                pct_change_1d >= MIN_PCT_CHANGE_1D
                and rel_vol >= MIN_RELATIVE_VOLUME
                and atr_pct >= MIN_ATR_PCT
            )
            volatility_ok = ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX
            h, l_, c = float(row["High"]), float(row["Low"]), close
            vwap = (h + l_ + c) / 3.0
            dist_vwap = (c - vwap) / vwap * 100 if vwap else 0.0
            structural_ok = close > vwap and dist_vwap <= MAX_DISTANCE_FROM_VWAP_PCT
            eligible = all([liquidity_ok, price_ok, momentum_ok, volatility_ok, structural_ok])
            score = pct_change_1d * 2.0 + (rel_vol - 1.0) * 10.0 + atr_pct
            results.append({
                "Date": date.date(),
                "Ticker": ticker,
                "Close": close,
                "Volume": today_vol,
                "pct_change_1d": pct_change_1d,
                "rel_vol": rel_vol,
                "ATR_pct": atr_pct,
                "Score": score,
                "Liquidity OK": liquidity_ok,
                "Price OK": price_ok,
                "Momentum OK": momentum_ok,
                "Volatility OK": volatility_ok,
                "Structural OK": structural_ok,
                "Eligible": eligible,
            })
    return pd.DataFrame(results)

# -----------------------------
# 5. Apply rules and save to data/YYYY/
# -----------------------------
ticker_list = list(data_frames.keys())
v26_results = v26_rules(data, ticker_list)

if v26_results.empty or "Eligible" not in v26_results.columns:
    v26_results = pd.DataFrame(columns=[
        "Date", "Ticker", "Close", "Volume", "pct_change_1d", "rel_vol", "ATR_pct", "Score",
        "Liquidity OK", "Price OK", "Momentum OK", "Volatility OK", "Structural OK", "Eligible"
    ])
    print("No data from IB or no eligible rows (check ticker list and dates).")

rule_path = os.path.join(year_dir, "v26_full_rule_table_sp500.csv")
v26_results.to_csv(rule_path, index=False)
print(f"Full rule table saved to '{rule_path}'")

TOP_PCT_PER_DAY = 1.0
triggered = v26_results[v26_results["Eligible"] == True].copy()
if not triggered.empty and "Score" in triggered.columns and TOP_PCT_PER_DAY < 1.0:
    triggered = triggered.copy()
    triggered["_rank"] = triggered.groupby("Date")["Score"].rank(method="first", ascending=False)
    triggered["_n"] = triggered.groupby("Date")["Date"].transform("count")
    keep = triggered["_rank"] <= triggered["_n"].mul(TOP_PCT_PER_DAY).clip(lower=1)
    triggered = triggered.loc[keep].drop(columns=["_rank", "_n"])
    print(f"Filtered to top {int(TOP_PCT_PER_DAY*100)}% per day: {len(triggered)} triggers")

triggered_path = os.path.join(year_dir, "v26_triggered_stocks_sp500.csv")
triggered.to_csv(triggered_path, index=False)
print(f"Triggered stocks saved to '{triggered_path}'")

if not triggered.empty:
    summary = triggered.groupby("Date")["Ticker"].count().reset_index()
    summary = summary.rename(columns={"Ticker": "Eligible Stocks"})
else:
    summary = pd.DataFrame(columns=["Date", "Eligible Stocks"])
summary_path = os.path.join(year_dir, "v26_daily_summary_sp500.csv")
summary.to_csv(summary_path, index=False)
print(f"Daily summary saved to '{summary_path}'")
print("Done.")
