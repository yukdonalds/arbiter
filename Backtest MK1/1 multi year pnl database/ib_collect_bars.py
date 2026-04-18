# -----------------------------
# IB 15m Bar Collection – Multi-Year Database
# -----------------------------
# Asks which year to collect. Reads triggered stocks from data/YYYY/,
# fetches 15m bars from TWS, and saves to data/YYYY/ (combined + per-ticker).
# Requires: TWS running, API enabled. Pip: pip install pfund-ibapi pytz
# -----------------------------

import os
import sys
import csv
import threading
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

try:
    import pytz
    NY = pytz.timezone("America/New_York")
except ImportError:
    NY = None
    print("Warning: pip install pytz for reliable endDateTime. Proceeding with empty endDateTime.")

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
except ImportError:
    print("Need: pip install pfund-ibapi  (or official ibapi)")
    sys.exit(1)

HOST = "127.0.0.1"
PORT = 7497
WORKERS = 2
TIMEOUT = 45
REQUEST_RETRIES = 2   # retry twice with primary client (3 attempts total)
ALTERNATE_CLIENT_RETRIES = 2  # then try alternate client ID this many times
RECONNECT_SLEEP = 10
MAX_TIMEOUTS_PER_SYMBOL = 1  # after first failed month (after primary + alternate), skip remaining months and drop ticker
SLEEP_BETWEEN_MONTHS = 0.12
SLEEP_BETWEEN_TICKERS = 0.2

BAR_SIZE = "15 mins"
DURATION_PER_REQUEST = "1 M"
USE_RTH = 1

# -----------------------------
# 1. Ask which year to collect
# -----------------------------
def get_year():
    while True:
        raw = input("What year to collect 15m bars for? (e.g. 2022): ").strip()
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
year_dir = os.path.join(DATA_DIR, str(YEAR))
triggered_path = os.path.join(year_dir, "v26_triggered_stocks_sp500.csv")
if not os.path.isfile(triggered_path):
    print(f"Missing {triggered_path}. Run run_validation.py for {YEAR} first.")
    sys.exit(1)

os.makedirs(year_dir, exist_ok=True)
COMBINED_CSV = os.path.join(year_dir, "ib_bars_15m_combined.csv")

def us_stock_contract(symbol: str) -> Contract:
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


class HistoricalDataApp(EWrapper, EClient):
    def __init__(self):
        self.connected = threading.Event()
        self.bars = []
        self.done = threading.Event()
        self.errors = []
        self.req_id = None
        EClient.__init__(self, self)

    def nextValidId(self, orderId: int):
        self.connected.set()

    def historicalData(self, reqId, bar):
        self.bars.append(bar)

    def historicalDataEnd(self, reqId, start, end):
        self.done.set()

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        self.errors.append((errorCode, errorString))
        if reqId >= 8000:
            self.done.set()
        if errorCode in (2104, 2106, 2158):
            return
        if errorCode == 162:
            return
        print(f"  Error {errorCode}: {errorString}")


def end_datetime_for_month(year: int, month: int) -> str:
    if not NY:
        return ""
    if month == 12:
        next_first = datetime(year + 1, 1, 1, tzinfo=NY)
    else:
        next_first = datetime(year, month + 1, 1, tzinfo=NY)
    end = next_first - timedelta(days=1)
    end = end.replace(hour=16, minute=0, second=0, microsecond=0)
    return end.strftime("%Y%m%d %H:%M:%S US/Eastern")


CONNECTION_EXCEPTIONS = (TypeError, OSError, ConnectionError, BrokenPipeError)

def fetch_15m_bars(symbol: str, app: HistoricalDataApp, end_dt: str, duration: str = DURATION_PER_REQUEST):
    app.bars = []
    app.done.clear()
    app.errors = []
    req_id = 8000 + hash(symbol) % 100000
    contract = us_stock_contract(symbol)
    try:
        app.reqHistoricalData(
            req_id,
            contract,
            end_dt,
            duration,
            BAR_SIZE,
            "TRADES",
            USE_RTH,
            1,
            False,
            []
        )
    except CONNECTION_EXCEPTIONS:
        return None
    if not app.done.wait(timeout=TIMEOUT):
        # timeout: print so user sees which symbol; worker number not available here
        print(f"  Timeout on {symbol}, retrying...", flush=True)
        return None
    return app.bars


def bar_to_row(bar, symbol: str) -> dict:
    date_str = getattr(bar, "date", "")
    open_ = getattr(bar, "open", 0) or 0
    high = getattr(bar, "high", 0) or 0
    low = getattr(bar, "low", 0) or 0
    close = getattr(bar, "close", 0) or 0
    volume = getattr(bar, "volume", 0) or 0
    return {
        "DateTime": date_str,
        "Ticker": symbol,
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": int(volume),
    }


def get_tickers():
    import pandas as pd
    df = pd.read_csv(triggered_path)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    start_d = f"{YEAR}-01-01"
    end_d = f"{YEAR}-12-31"
    mask = (df["Date"] >= start_d) & (df["Date"] <= end_d)
    tickers = df.loc[mask, "Ticker"].str.replace(".US", "").drop_duplicates().tolist()
    if not tickers:
        return ["AAPL", "MSFT", "GOOG", "SPY"]
    return tickers


def get_tickers_to_fetch(tickers_all: list) -> list:
    """Return tickers that do not yet have a saved _15m.csv (resume: skip already done)."""
    to_fetch = []
    for t in tickers_all:
        path = os.path.join(year_dir, f"{t}_15m.csv")
        if not os.path.isfile(path):
            to_fetch.append(t)
    return to_fetch


def load_all_rows_from_disk() -> list:
    """Load all rows from existing *_15m.csv files in year_dir (for rebuilding combined)."""
    rows = []
    fieldnames = ["DateTime", "Ticker", "Open", "High", "Low", "Close", "Volume"]
    for fname in sorted(os.listdir(year_dir)):
        if fname.endswith("_15m.csv") and fname != "ib_bars_15m_combined.csv":
            path = os.path.join(year_dir, fname)
            try:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    r = csv.DictReader(f, fieldnames=fieldnames)
                    next(r, None)  # skip header
                    for row in r:
                        if row.get("DateTime"):
                            rows.append(row)
            except Exception as e:
                print(f"  Warning: could not read {fname}: {e}")
    return rows


def connect_app(client_id: int):
    app = HistoricalDataApp()
    app.connect(HOST, PORT, clientId=client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    if not app.connected.wait(timeout=20):
        try:
            app.disconnect()
        except Exception:
            pass
        return None
    return app


def reconnect_app(app, client_id: int):
    try:
        app.disconnect()
    except Exception:
        pass
    time.sleep(RECONNECT_SLEEP)
    return connect_app(client_id)


def worker_fetch_tickers(ticker_chunk: list, client_id: int, alternate_client_id: int, worker_num: int = 0, progress_counter: list = None, progress_lock: threading.Lock = None) -> list:
    app = connect_app(client_id)
    if app is None:
        print(f"  Worker {worker_num}: could not connect to TWS (port {PORT}?).", flush=True)
        return []
    ticker_list = ", ".join(ticker_chunk) if len(ticker_chunk) <= 7 else ", ".join(ticker_chunk[:5] + ["..."] + ticker_chunk[-2:])
    print(f"  Worker {worker_num} connected (clientId {client_id}), fetching {len(ticker_chunk)} tickers: {ticker_list}", flush=True)
    rows_for_combined = []
    for symbol in ticker_chunk:
        symbol_bars = []
        timeouts_this_symbol = 0
        for month in range(1, 13):
            if app is None:
                app = connect_app(client_id)
                if app is None:
                    break
            end_dt = end_datetime_for_month(YEAR, month)
            bars = None
            for attempt in range(REQUEST_RETRIES):
                bars = fetch_15m_bars(symbol, app, end_dt, DURATION_PER_REQUEST)
                if bars is not None:
                    break
                app = reconnect_app(app, client_id)
            # if still no data, try alternate client ID (e.g. different TWS connection)
            if bars is None and alternate_client_id is not None:
                try:
                    if app is not None:
                        try:
                            app.disconnect()
                        except Exception:
                            pass
                        time.sleep(RECONNECT_SLEEP)
                    app_alt = connect_app(alternate_client_id)
                    if app_alt is not None:
                        for _ in range(ALTERNATE_CLIENT_RETRIES):
                            bars = fetch_15m_bars(symbol, app_alt, end_dt, DURATION_PER_REQUEST)
                            if bars is not None:
                                break
                            app_alt = reconnect_app(app_alt, alternate_client_id)
                        try:
                            app_alt.disconnect()
                        except Exception:
                            pass
                        time.sleep(RECONNECT_SLEEP)
                except Exception:
                    pass
                app = connect_app(client_id)
            if bars is None:
                timeouts_this_symbol += 1
                if timeouts_this_symbol >= MAX_TIMEOUTS_PER_SYMBOL:
                    print(f"  Worker {worker_num}: dropping {symbol} (too many timeouts, primary + alternate failed)", flush=True)
                    if progress_lock is not None and progress_counter is not None:
                        remaining = 12 - month
                        with progress_lock:
                            progress_counter[0] += remaining
                    break
            if bars:
                symbol_bars.extend(bars)
            if progress_lock is not None and progress_counter is not None:
                with progress_lock:
                    progress_counter[0] += 1
            time.sleep(SLEEP_BETWEEN_MONTHS)
        if not symbol_bars:
            time.sleep(SLEEP_BETWEEN_TICKERS)
            continue
        rows = [bar_to_row(b, symbol) for b in symbol_bars]
        rows.sort(key=lambda r: r["DateTime"])
        rows_for_combined.extend(rows)
        ticker_csv = os.path.join(year_dir, f"{symbol}_15m.csv")
        with open(ticker_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["DateTime", "Ticker", "Open", "High", "Low", "Close", "Volume"])
            w.writeheader()
            w.writerows(rows)
        time.sleep(SLEEP_BETWEEN_TICKERS)
    if app is not None:
        try:
            app.disconnect()
        except Exception:
            pass
    return rows_for_combined


def main():
    tickers_all = get_tickers()
    tickers_to_fetch = get_tickers_to_fetch(tickers_all)
    skipped = len(tickers_all) - len(tickers_to_fetch)
    if skipped:
        print(f"Resume: skipping {skipped} ticker(s) already saved in {year_dir}")
    if not tickers_to_fetch:
        print("All tickers already collected. Rebuilding combined CSV from disk...")
        all_rows = load_all_rows_from_disk()
        if all_rows:
            all_rows.sort(key=lambda r: (r["DateTime"], r["Ticker"]))
            with open(COMBINED_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["DateTime", "Ticker", "Open", "High", "Low", "Close", "Volume"])
                w.writeheader()
                w.writerows(all_rows)
            print(f"Combined: {len(all_rows)} rows -> {COMBINED_CSV}")
        print("Done.")
        return

    n_workers = max(1, min(WORKERS, 8))
    def chunk(lst, n):
        size = (len(lst) + n - 1) // n
        return [lst[i : i + size] for i in range(0, len(lst), size)]
    chunks = chunk(tickers_to_fetch, n_workers)
    client_ids = [1 + i for i in range(n_workers)]
    alternate_client_ids = [3 + i for i in range(n_workers)]  # try alternate client if primary fails
    progress_total_requests = len(tickers_to_fetch) * 12
    print(f"IB 15m bars – year {YEAR}  {HOST}:{PORT}  {n_workers} workers")
    print(f"Tickers to fetch: {len(tickers_to_fetch)}  (of {len(tickers_all)} total)  Output: {year_dir}")
    print(f"Combined CSV: {COMBINED_CSV}\n")
    sys.stdout.flush()

    progress_counter = [0]
    progress_lock = threading.Lock()
    progress_stop = threading.Event()

    def progress_thread_fn():
        while not progress_stop.wait(5):
            with progress_lock:
                n = progress_counter[0]
            tickers_done = n // 12
            print(f"Progress: {n}/{progress_total_requests} requests (~{tickers_done}/{len(tickers_to_fetch)} tickers)", flush=True)
        with progress_lock:
            n = progress_counter[0]
        tickers_done = n // 12
        print(f"Progress: {n}/{progress_total_requests} requests (~{tickers_done}/{len(tickers_to_fetch)} tickers)", flush=True)

    progress_thread = threading.Thread(target=progress_thread_fn, daemon=True)
    progress_thread.start()

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(worker_fetch_tickers, chunks[i], client_ids[i], alternate_client_ids[i], i + 1, progress_counter, progress_lock): i
            for i in range(len(chunks))
        }
        for future in as_completed(futures):
            worker_idx = futures[future]
            try:
                future.result()
                print(f"  Worker {worker_idx + 1} finished")
            except Exception as e:
                print(f"  Worker {worker_idx + 1} failed: {e}")
            sys.stdout.flush()
    progress_stop.set()
    progress_thread.join(timeout=6)

    # Rebuild combined from all per-ticker CSVs (existing + newly fetched)
    print("\nRebuilding combined CSV from all per-ticker files...")
    all_rows = load_all_rows_from_disk()
    if all_rows:
        all_rows.sort(key=lambda r: (r["DateTime"], r["Ticker"]))
        with open(COMBINED_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["DateTime", "Ticker", "Open", "High", "Low", "Close", "Volume"])
            w.writeheader()
            w.writerows(all_rows)
        print(f"Combined: {len(all_rows)} rows -> {COMBINED_CSV}")
    print("Done.")


if __name__ == "__main__":
    main()
