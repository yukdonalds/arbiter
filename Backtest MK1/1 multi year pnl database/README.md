# Multi-Year PnL Database

Run validation and data collection **by year**, then run PnL over any **date range** with any **starting capital**.

## Folder layout

- **data/** – All inputs and raw data, stored by year:
  - **data/2022/** – Triggered stocks, rule table, daily summary, and IB 15m bars for 2022
  - **data/2023/** – Same for 2023
  - (one folder per year you run)

## Workflow

### 1. Validation (which year?)

```bash
python run_validation.py
```

- Prompts: **What year to run?** (e.g. 2022)
- Downloads S&P 500 daily data for that year, applies v2.6 rules
- Writes to **data/YYYY/**:
  - `v26_full_rule_table_sp500.csv`
  - `v26_triggered_stocks_sp500.csv`
  - `v26_daily_summary_sp500.csv`

### 2. IB bar collection (which year?)

```bash
python ib_collect_bars.py
```

- Prompts: **What year to collect 15m bars for?** (e.g. 2022)
- Requires **data/YYYY/v26_triggered_stocks_sp500.csv** (run validation for that year first)
- Fetches 15m bars from TWS and writes to **data/YYYY/**:
  - `ib_bars_15m_combined.csv`
  - `SYMBOL_15m.csv` (one per ticker)

### 3. PnL backtest (start date, end date, capital)

```bash
python pnl_ib_bars.py
```

- Prompts:
  - **Start date (YYYY-MM-DD)**
  - **End date (YYYY-MM-DD)**
  - **Starting capital** (e.g. 1000)
- Loads triggered stocks and IB bars from **data/YYYY/** for every year in the range
- Runs backtest and writes in this folder:
  - `pnl_results_YYYYMMDD_YYYYMMDD.csv`
  - `pnl_daily_summary_YYYYMMDD_YYYYMMDD.csv`
  - `pnl_report_YYYYMMDD_YYYYMMDD.txt`

You can change start/end and capital and re-run without re-fetching data.

## Requirements

- **run_validation.py**: `yfinance`, `pandas`, `requests`, `beautifulsoup4`
- **ib_collect_bars.py**: TWS running, API enabled; `pfund-ibapi` (or `ibapi`), `pytz`
- **pnl_ib_bars.py**: `pandas` (only)
