# -----------------------------
# Fast Paper Trader – Fetch S&P 500 tickers from IB (qualify seed list)
# IB doesn't provide S&P 500 constituents directly. We qualify our seed list via IB
# and save IB's canonical symbols, so no "unrecognized" errors.
# -----------------------------
"""
Run: python fetch_sp500_from_ib.py
Requires: TWS/IB Gateway running, API enabled (port 7497 paper).
Seed: sp500_tickers.txt (Wikipedia or any S&P 500 list).
Output: Overwrites sp500_tickers.txt with IB-qualified symbols only.
"""
import time
from ib_insync import IB, Stock
import config
from ib_connection import connect_ib, disconnect_ib

BATCH_SIZE = 40
SLEEP_BETWEEN_BATCHES = 1.5


def load_seed_tickers() -> list[str]:
    out = []
    with open(config.SP500_TICKERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.upper())
    return out


def make_stock_for_qualify(symbol: str) -> Stock:
    """IB uses hyphen for share classes (BRK-B). Pass both forms; IB will resolve."""
    sym = symbol.replace(".", "-")
    return Stock(sym, "SMART", "USD")


def main():
    print("Fetching S&P 500 tickers from IB (qualifying seed list)...")
    seed = load_seed_tickers()
    if not seed:
        print("No seed tickers in sp500_tickers.txt.")
        return
    print(f"Seed list: {len(seed)} tickers")

    ib = connect_ib()
    ib.reqMarketDataType(getattr(config, "MARKET_DATA_TYPE", 3))
    ib.sleep(1)

    qualified_symbols = []
    failed = []
    n = len(seed)

    for i in range(0, n, BATCH_SIZE):
        batch = seed[i : i + BATCH_SIZE]
        contracts = [make_stock_for_qualify(s) for s in batch]
        try:
            results = ib.qualifyContracts(*contracts)
            for c in results:
                qualified_symbols.append(c.symbol)
            for s, c in zip(batch, contracts):
                if c not in results:
                    failed.append(s)
        except Exception as e:
            print(f"  Batch error at {i}: {e}")
            for s in batch:
                failed.append(s)
        if (i + BATCH_SIZE) < n:
            time.sleep(SLEEP_BETWEEN_BATCHES)
        print(f"  Qualified {len(qualified_symbols)} / {i + len(batch)}...", flush=True)

    disconnect_ib(ib)

    # Write back IB-qualified symbols only
    with open(config.SP500_TICKERS_FILE, "w", encoding="utf-8") as f:
        f.write("# S&P 500 tickers (IB-qualified via fetch_sp500_from_ib.py)\n")
        f.write("# Run fetch_sp500_from_ib.py periodically to refresh.\n")
        for s in qualified_symbols:
            f.write(s + "\n")

    print(f"Done. Wrote {len(qualified_symbols)} IB-qualified tickers to {config.SP500_TICKERS_FILE}")
    if failed:
        print(f"Failed to qualify: {len(failed)} tickers: {', '.join(failed[:20])}{'...' if len(failed) > 20 else ''}")


if __name__ == "__main__":
    main()

