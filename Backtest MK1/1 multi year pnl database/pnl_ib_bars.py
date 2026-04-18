# -----------------------------
# v2.6 P&L from IB 15m Bars – Multi-Year Database
# -----------------------------
# Asks start date, end date, and starting capital. Loads triggered stocks and
# IB bars from data/YYYY/ for each year in range, runs backtest, saves results.
# -----------------------------

import os
import sys
import random
from datetime import datetime, timedelta
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CSV_DIR = os.path.join(BASE_DIR, "csv")
REPORT_DIR = os.path.join(BASE_DIR, "report")

# -----------------------------
# 1. Ask start date, end date, starting capital
# -----------------------------2022-01-01
def parse_date(s):
    try:
        return pd.Timestamp(s).normalize()
    except Exception:
        return None

def get_start_date():
    while True:
        raw = input("Start date (YYYY-MM-DD): ").strip()
        if raw:
            d = parse_date(raw)
            if d is not None:
                return d.strftime("%Y-%m-%d")
        print("  Please enter a valid date (e.g. 2022-01-01).")

def get_end_date():
    while True:
        raw = input("End date (YYYY-MM-DD): ").strip()
        if raw:
            d = parse_date(raw)
            if d is not None:
                return d.strftime("%Y-%m-%d")
        print("  Please enter a valid date (e.g. 2022-12-31).")

def get_capital():
    while True:
        raw = input("Starting capital (e.g. 1000): ").strip()
        if not raw:
            raw = "1000"
        try:
            c = float(raw.replace(",", ""))
            if c > 0:
                return c
        except ValueError:
            pass
        print("  Please enter a positive number.")

BACKTEST_START = get_start_date()
BACKTEST_END = get_end_date()
start_d = pd.Timestamp(BACKTEST_START).normalize()
end_d = pd.Timestamp(BACKTEST_END).normalize()
if start_d > end_d:
    print("Start date must be on or before end date.")
    sys.exit(1)

initial_capital = get_capital()
print(f"\nBacktest: {BACKTEST_START} to {BACKTEST_END}  Capital: ${initial_capital:,.2f}\n")

# -----------------------------
# 2. Which years do we need?
# -----------------------------
start_year = start_d.year
end_year = end_d.year
years_needed = list(range(start_year, end_year + 1))

# Load triggered from each year and concatenate (filter to date range)
triggered_dfs = []
for year in years_needed:
    year_dir = os.path.join(DATA_DIR, str(year))
    triggered_path = os.path.join(year_dir, "v26_triggered_stocks_sp500.csv")
    if not os.path.isfile(triggered_path):
        print(f"Missing {triggered_path}. Run run_validation.py for {year} first.")
        sys.exit(1)
    df = pd.read_csv(triggered_path)
    df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
    df = df[(df["Date"] >= start_d) & (df["Date"] <= end_d)]
    if not df.empty:
        triggered_dfs.append(df)

if not triggered_dfs:
    print(f"No triggered rows in {BACKTEST_START} to {BACKTEST_END}. Run validation for those years.")
    sys.exit(1)

triggered = pd.concat(triggered_dfs, ignore_index=True).drop_duplicates().sort_values("Date").reset_index(drop=True)

# -----------------------------
# 2b. Full rule table for "last trading day top 100" watchlist (same as live)
# -----------------------------
def prev_trading_day(d):
    """Return previous trading day (weekday before d). d is date or Timestamp."""
    t = pd.Timestamp(d).normalize()
    prev = t - timedelta(days=1)
    while prev.weekday() >= 5:  # Saturday=5, Sunday=6
        prev -= timedelta(days=1)
    return prev

USE_WATCHLIST = True
WATCHLIST_TOP_N = 100

full_rule_dfs = []
for year in years_needed:
    year_dir = os.path.join(DATA_DIR, str(year))
    rule_path = os.path.join(year_dir, "v26_full_rule_table_sp500.csv")
    if os.path.isfile(rule_path):
        fr = pd.read_csv(rule_path)
        fr["Date"] = pd.to_datetime(fr["Date"]).dt.normalize()
        full_rule_dfs.append(fr)
# Also load prior year for first backtest day's prev (e.g. 2022-01-01 -> prev 2021-12-31)
prior_year = start_year - 1
if prior_year not in years_needed:
    prior_dir = os.path.join(DATA_DIR, str(prior_year))
    rule_path_prior = os.path.join(prior_dir, "v26_full_rule_table_sp500.csv")
    if os.path.isfile(rule_path_prior):
        fr = pd.read_csv(rule_path_prior)
        fr["Date"] = pd.to_datetime(fr["Date"]).dt.normalize()
        full_rule_dfs.append(fr)

if full_rule_dfs:
    full_rule = pd.concat(full_rule_dfs, ignore_index=True)
    if "Score" not in full_rule.columns:
        full_rule["Score"] = 0.0
    full_rule["Score"] = pd.to_numeric(full_rule["Score"], errors="coerce").fillna(0.0)
    # For each date D in triggered, watchlist_D = top WATCHLIST_TOP_N by Score where Date == prev(D)
    watchlist_by_date = {}
    for d in triggered["Date"].dropna().unique():
        date_str = pd.Timestamp(d).strftime("%Y-%m-%d")
        prev = prev_trading_day(d)
        prev_str = prev.strftime("%Y-%m-%d")
        rows = full_rule[full_rule["Date"] == prev]
        if rows.empty:
            continue  # no filter for this day (include all tickers)
        top = rows.nlargest(WATCHLIST_TOP_N, "Score")
        watchlist_by_date[date_str] = set(top["Ticker"].astype(str).str.replace(".US", "").str.strip())
    if USE_WATCHLIST:
        print(f"Watchlist: last trading day top {WATCHLIST_TOP_N} (same as live). {len(watchlist_by_date)} days with watchlist.")
else:
    watchlist_by_date = {}
    if USE_WATCHLIST:
        print("Watchlist: v26_full_rule_table not found; running without watchlist filter.")

if USE_WATCHLIST and watchlist_by_date:
    def in_watchlist(r):
        ds = pd.Timestamp(r["Date"]).strftime("%Y-%m-%d")
        t = str(r["Ticker"]).replace(".US", "").strip()
        if ds not in watchlist_by_date:
            return True
        return t in watchlist_by_date[ds]
    mask = triggered.apply(in_watchlist, axis=1)
    n_before = len(triggered)
    triggered = triggered.loc[mask].reset_index(drop=True)
    print(f"Triggered rows after watchlist filter: {len(triggered)} (was {n_before})")

# Load IB bars from each year and merge into one index
bars_dfs = []
for year in years_needed:
    year_dir = os.path.join(DATA_DIR, str(year))
    ib_csv = os.path.join(year_dir, "ib_bars_15m_combined.csv")
    if not os.path.isfile(ib_csv):
        print(f"Missing {ib_csv}. Run ib_collect_bars.py for {year} first.")
        sys.exit(1)
    bars_dfs.append(pd.read_csv(ib_csv))

bars_df = pd.concat(bars_dfs, ignore_index=True)

# -----------------------------
# 3. Triggered: Score, ATR_pct, SizeMult
# -----------------------------
if "Score" not in triggered.columns:
    triggered["Score"] = 0.0
if "ATR_pct" not in triggered.columns:
    triggered["ATR_pct"] = 2.0
triggered["Score"] = pd.to_numeric(triggered["Score"], errors="coerce").fillna(0.0)
triggered["ATR_pct"] = pd.to_numeric(triggered["ATR_pct"], errors="coerce").fillna(2.0)

def _size_mult(score_series):
    if score_series.empty or score_series.nunique() == 0:
        return pd.Series(1.0, index=score_series.index)
    q70 = score_series.quantile(0.70)
    q50 = score_series.quantile(0.50)
    def mult(s):
        if s >= q70: return 1.2
        if s <= q50: return 0.8
        return 1.0
    return score_series.map(mult)
triggered["SizeMult"] = triggered.groupby("Date")["Score"].transform(_size_mult)

# -----------------------------
# 4. Parameters
# -----------------------------
max_stocks_per_day = 10
max_position_pct_capital = 0.15
target_pct_original = 0.05
stop_pct_original = 0.03
min_triggers_per_day = 1
# Watchlist: only trade tickers in "last trading day top N" (same as live main)
# USE_WATCHLIST and WATCHLIST_TOP_N are set above after loading full rule table.
ibkr_per_share = 0.0035
ibkr_min_per_side = 1.00
ibkr_max_pct_of_trade = 0.005
slippage_pct = 0.002
slippage_volatile_pct = 0.005
volatile_threshold = 0.03
adverse_fill_noise_pct = 0.003
target_miss_pct = 0.10
gap_through_stop_pct = 0.30
gap_through_worse_pct = 0.005
realism_seed = 42

date_trigger_count = triggered.groupby(triggered["Date"].dt.strftime("%Y-%m-%d")).size()
n_total = len(triggered)
capital = float(initial_capital)
random.seed(realism_seed)
current_date = None
day_pnl = 0.0
stocks_traded_today = 0

# -----------------------------
# 5. Parse bars and index by (date_str, ticker)
# -----------------------------
raw_dt = bars_df["DateTime"].astype(str).str.strip()
bars_df["_dt_naive"] = pd.to_datetime(raw_dt.str.replace(r"\s+US/Eastern$", "", regex=True), format="%Y%m%d %H:%M:%S", errors="coerce")
bars_df = bars_df.dropna(subset=["_dt_naive"])
bars_df["_date_str"] = bars_df["_dt_naive"].dt.strftime("%Y-%m-%d")
bars_df = bars_df.sort_values(["Ticker", "_dt_naive"]).reset_index(drop=True)

bars_by_date_ticker = {}
for (date_str, ticker), grp in bars_df.groupby([bars_df["_date_str"], bars_df["Ticker"]]):
    key = (str(date_str), str(ticker).strip())
    bars_by_date_ticker[key] = grp.reset_index(drop=True)
bars_df.drop(columns=["_dt_naive", "_date_str"], inplace=True)

print(f"Loaded {len(bars_df)} bars, {len(bars_by_date_ticker)} (date,ticker) day-bars.")
print(f"Triggered rows in range: {n_total}\n")
print(f"Position: min({max_position_pct_capital*100:.0f}%, 1/N) per trade, total <= 100%\n")

def _col(df, name):
    if name in df.columns:
        return name
    if name.lower() in df.columns:
        return name.lower()
    return name

def _scalar(x):
    if hasattr(x, "iloc"):
        x = x.iloc[0] if len(x) else 0.0
    return float(x)

pnl_results = []

# -----------------------------
# 6. Loop over triggered rows; run exit logic
# -----------------------------
for idx, row in triggered.iterrows():
    i = idx + 1
    ticker = str(row["Ticker"]).replace(".US", "").strip()
    date = row["Date"]
    date_str = pd.Timestamp(date).strftime("%Y-%m-%d")
    if date != current_date:
        if current_date is not None:
            capital += day_pnl
        current_date = date
        day_pnl = 0.0
        stocks_traded_today = 0
    if stocks_traded_today >= max_stocks_per_day:
        continue
    n_triggers_today = date_trigger_count.get(date_str, 0)
    if n_triggers_today < min_triggers_per_day:
        continue
    capital_for_today = capital
    n_stocks_today = min(n_triggers_today, max_stocks_per_day)
    size_mult = float(row.get("SizeMult", 1.0))

    hist = bars_by_date_ticker.get((date_str, ticker))
    if hist is None or len(hist) < 1:
        print(f"  [{i}/{n_total}] {date_str} {ticker}... no bar data")
        continue

    ocol, hcol, lcol, ccol = _col(hist, "Open"), _col(hist, "High"), _col(hist, "Low"), _col(hist, "Close")
    open_price = _scalar(hist.iloc[0][ocol])
    high_price = _scalar(hist[hcol].max())
    low_price = _scalar(hist[lcol].min())
    close_price = _scalar(hist.iloc[-1][ccol])
    if not open_price or open_price <= 0:
        print(f"  [{i}/{n_total}] {date_str} {ticker}... no open")
        continue

    entry_price = open_price
    position_pct = min(max_position_pct_capital, 1.0 / n_stocks_today)
    total_position_size = capital_for_today * position_pct
    shares = total_position_size / entry_price if entry_price > 0 else 0
    if shares <= 0:
        continue
    stop_price = entry_price * (1 - stop_pct_original)
    target_price = entry_price * (1 + target_pct_original)
    exit_price_actual = close_price
    exit_reason = "Close"
    n_bars = len(hist)
    exit_bar = n_bars - 1  # default: exit at last bar (EOD)
    if n_bars == 1:
        exit_bar = 0
        h, l = _scalar(hist.iloc[0][hcol]), _scalar(hist.iloc[0][lcol])
        if l <= stop_price and h >= target_price:
            exit_price_actual = stop_price
            exit_reason = "Stop Hit"
        elif l <= stop_price:
            o = _scalar(hist.iloc[0][ocol])
            exit_price_actual = o if o < stop_price else stop_price
            exit_reason = "Stop Hit"
        elif h >= target_price:
            exit_price_actual = target_price
            exit_reason = "Target Hit"
    else:
        for b in range(1, n_bars):
            row_b = hist.iloc[b]
            h, l = _scalar(row_b[hcol]), _scalar(row_b[lcol])
            if l <= stop_price and h >= target_price:
                exit_price_actual = stop_price
                exit_reason = "Stop Hit"
                exit_bar = b
                break
            if l <= stop_price:
                o = _scalar(row_b[ocol])
                exit_price_actual = o if o < stop_price else stop_price
                exit_reason = "Stop Hit"
                exit_bar = b
                break
            if h >= target_price:
                exit_price_actual = target_price
                exit_reason = "Target Hit"
                exit_bar = b
                break
    if exit_reason == "Target Hit" and random.random() < target_miss_pct:
        exit_price_actual = close_price
        exit_reason = "Target Miss (Close)"
    if exit_reason == "Stop Hit" and random.random() < gap_through_stop_pct:
        first_open = _scalar(hist.iloc[0][ocol])
        if first_open < stop_price:
            exit_price_actual = min(exit_price_actual, first_open)
        else:
            exit_price_actual = exit_price_actual * (1 - gap_through_worse_pct)

    day_range_pct = (high_price - low_price) / open_price if open_price else 0
    slippage_today = slippage_volatile_pct if day_range_pct >= volatile_threshold else slippage_pct
    entry_actual = entry_price * (1 + slippage_today)
    exit_actual = exit_price_actual * (1 - slippage_today)
    entry_actual *= (1 + random.uniform(0, adverse_fill_noise_pct))
    exit_actual *= (1 - random.uniform(0, adverse_fill_noise_pct))

    buy_value = shares * entry_actual
    sell_value = shares * exit_actual
    buy_comm = min(max(ibkr_min_per_side, shares * ibkr_per_share), ibkr_max_pct_of_trade * buy_value)
    sell_comm = min(max(ibkr_min_per_side, shares * ibkr_per_share), ibkr_max_pct_of_trade * sell_value)
    total_commission = buy_comm + sell_comm

    pnl = (exit_actual - entry_actual) * shares - total_commission
    day_pnl += pnl
    stocks_traded_today += 1

    pnl_results.append({
        "Date": date_str,
        "Ticker": ticker,
        "Capital_Used": round(capital_for_today, 2),
        "Entry": round(entry_price, 2),
        "Exit": round(exit_price_actual, 2),
        "Entry_Actual": round(entry_actual, 2),
        "Exit_Actual": round(exit_actual, 2),
        "Exit Reason": exit_reason,
        "ExitBar": exit_bar,
        "Shares": round(shares, 2),
        "Commission": round(total_commission, 2),
        "P&L": round(pnl, 2),
    })
    print(f"  [{i}/{n_total}] {date_str} {ticker}... {exit_reason}  P&L ${pnl:,.2f}")

capital += day_pnl

# -----------------------------
# 7. Save results and report (in database folder)
# -----------------------------
safe_start = BACKTEST_START.replace("-", "")
safe_end = BACKTEST_END.replace("-", "")
os.makedirs(CSV_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)
out_csv = os.path.join(CSV_DIR, f"pnl_results_{safe_start}_{safe_end}.csv")
daily_csv = os.path.join(CSV_DIR, f"pnl_daily_summary_{safe_start}_{safe_end}.csv")
report_path = os.path.join(REPORT_DIR, f"pnl_report_{safe_start}_{safe_end}.txt")

pnl_df = pd.DataFrame(pnl_results)
pnl_df.to_csv(out_csv, index=False)
print(f"\nResults saved to '{out_csv}'")

if not pnl_df.empty:
    daily = pnl_df.groupby("Date", as_index=False)["P&L"].sum()
    daily = daily.sort_values("Date").reset_index(drop=True)
    daily["Cumulative P&L"] = daily["P&L"].cumsum()
    daily["Running_Capital"] = initial_capital + daily["Cumulative P&L"]
    daily.to_csv(daily_csv, index=False)
    print(f"Daily summary saved to '{daily_csv}'")

    n_trades = len(pnl_df)
    n_days = len(daily)
    total_pnl = float(capital - initial_capital)
    ret_pct = (total_pnl / initial_capital * 100) if initial_capital else 0.0
    avg_daily = (daily["P&L"].sum() / n_days) if n_days else 0.0
    best_idx = daily["P&L"].idxmax()
    worst_idx = daily["P&L"].idxmin()
    best_date = daily.loc[best_idx, "Date"]
    best_pnl = daily.loc[best_idx, "P&L"]
    worst_date = daily.loc[worst_idx, "Date"]
    worst_pnl = daily.loc[worst_idx, "P&L"]
    rc = daily["Running_Capital"]
    peak = rc.cummax()
    dd = peak - rc
    max_dd = float(dd.max()) if len(dd) else 0.0
    peak_at_trough = float(peak[dd.idxmax()]) if len(dd) and max_dd > 0 else 0.0
    max_dd_pct = (max_dd / peak_at_trough * 100) if peak_at_trough else 0.0
    wins = (pnl_df["P&L"] > 0).sum()
    losses = (pnl_df["P&L"] < 0).sum()
    win_rate = (wins / n_trades * 100) if n_trades else 0.0

    def _dstr(x):
        return x if isinstance(x, str) else pd.Timestamp(x).strftime("%Y-%m-%d")

    # ----- Quick wins: clustering and risk stats -----
    worst_5d = daily["P&L"].rolling(5, min_periods=1).sum()
    worst_5d_val = float(worst_5d.min()) if len(worst_5d) else 0.0
    worst_5d_idx = worst_5d.idxmin() if len(worst_5d) else None
    worst_5d_end_date = _dstr(daily.loc[worst_5d_idx, "Date"]) if worst_5d_idx is not None else ""

    stops_per_day = pnl_df[pnl_df["Exit Reason"] == "Stop Hit"].groupby("Date").size()
    stops_per_day = daily[["Date"]].merge(stops_per_day.rename("Stops"), left_on="Date", right_index=True, how="left")["Stops"].fillna(0).astype(int)
    max_stops_one_day = int(stops_per_day.max()) if len(stops_per_day) else 0
    d0 = (stops_per_day == 0).sum()
    d1 = (stops_per_day == 1).sum()
    d2 = (stops_per_day == 2).sum()
    d3p = (stops_per_day >= 3).sum()
    stops_dist_str = f"0 stops: {d0} days, 1: {d1}, 2: {d2}, 3+: {d3p}"

    pl_series = pnl_df["P&L"].values
    run_len, run_sum, run_end = 0, 0.0, -1
    cur_len, cur_sum = 0, 0.0
    for i in range(len(pl_series)):
        if pl_series[i] < 0:
            cur_len += 1
            cur_sum += pl_series[i]
            if cur_len > run_len or (cur_len == run_len and cur_sum < run_sum):
                run_len, run_sum, run_end = cur_len, cur_sum, i
        else:
            cur_len, cur_sum = 0, 0.0
    worst_run_count = run_len
    worst_run_pnl = run_sum

    window = 10
    worst_10_pnl = 0.0
    worst_10_start_idx = 0
    if len(pl_series) >= window:
        for i in range(len(pl_series) - window + 1):
            s = float(sum(pl_series[i:i + window]))
            if s < worst_10_pnl:
                worst_10_pnl = s
                worst_10_start_idx = i
    cap_before_run = initial_capital + (float(pnl_df["P&L"].iloc[:worst_10_start_idx].sum()) if worst_10_start_idx > 0 else 0)
    worst_10_pct = (worst_10_pnl / cap_before_run * 100) if cap_before_run > 0 else 0.0

    theoretical_one_day_pct = max_stocks_per_day * max_position_pct_capital * stop_pct_original * 100  # 4.5% with default params

    # ----- Intraday equity curve and max intraday DD -----
    start_caps = [initial_capital] + daily["Running_Capital"].iloc[:-1].tolist()
    daily = daily.copy()
    daily["Start_Capital"] = start_caps
    intraday_equity = []
    max_single_day_intraday_dd = 0.0
    max_single_day_intraday_dd_date = ""
    for d_idx, date_row in daily.iterrows():
        date_val = date_row["Date"]
        date_str = pd.Timestamp(date_val).strftime("%Y-%m-%d")
        start_cap = float(date_row["Start_Capital"])
        day_trades = pnl_df[pnl_df["Date"].astype(str).str[:10] == date_str].copy()
        if day_trades.empty:
            continue
        day_trades = day_trades.reset_index(drop=True)
        max_bar = int(day_trades["ExitBar"].max())
        day_equity = []
        for bar_idx in range(max_bar + 1):
            closed = day_trades[day_trades["ExitBar"] <= bar_idx]
            closed_pnl = float(closed["P&L"].sum())
            open_trades = day_trades[day_trades["ExitBar"] > bar_idx]
            unrealized = 0.0
            for _, tr in open_trades.iterrows():
                ticker = str(tr["Ticker"]).strip()
                key = (date_str, ticker)
                if key not in bars_by_date_ticker:
                    continue
                hist = bars_by_date_ticker[key]
                if bar_idx >= len(hist):
                    continue
                ccol = _col(hist, "Close")
                bar_close = float(hist.iloc[bar_idx][ccol])
                entry = float(tr["Entry"])
                sh = float(tr["Shares"])
                unrealized += (bar_close - entry) * sh
            eq = start_cap + closed_pnl + unrealized
            day_equity.append(eq)
            intraday_equity.append(eq)
        if day_equity:
            day_peak = max(day_equity)
            day_trough = min(day_equity)
            day_dd = day_peak - day_trough
            if day_dd > max_single_day_intraday_dd:
                max_single_day_intraday_dd = day_dd
                max_single_day_intraday_dd_date = date_str

    if intraday_equity:
        rc_intra = pd.Series(intraday_equity)
        peak_intra = rc_intra.cummax()
        dd_intra = peak_intra - rc_intra
        max_dd_intra = float(dd_intra.max())
        peak_at_trough_intra = float(peak_intra[dd_intra.idxmax()]) if max_dd_intra > 0 else 0.0
        max_dd_intra_pct = (max_dd_intra / peak_at_trough_intra * 100) if peak_at_trough_intra else 0.0
    else:
        max_dd_intra = 0.0
        max_dd_intra_pct = 0.0

    lines = [
        f"========================================================================\n  v2.6 P&L INTRADAY – IB 15m BARS ({BACKTEST_START} to {BACKTEST_END})\n========================================================================\n",
        f"\n  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
        f"  Data source: multi-year database data/ [{start_year}-{end_year}]\n",
        "------------------------------------------------------------------------\n  PARAMETERS\n------------------------------------------------------------------------\n",
        f"  Initial capital              ${initial_capital:,.2f}",
        f"  Backtest range               {BACKTEST_START} to {BACKTEST_END}",
        "",
        "  Capital / position rules",
        f"    Max stocks per day           {max_stocks_per_day}",
        f"    Max position per trade       {max_position_pct_capital*100:.0f}% of capital",
        f"    Position sizing              min({max_position_pct_capital*100:.0f}%, 1/N) per trade; total <= 100%",
        f"    Min triggers to trade day    {min_triggers_per_day}",
        f"    Watchlist (same as live)     last trading day top {WATCHLIST_TOP_N} tickers" if USE_WATCHLIST else "    Watchlist                     off (full universe)",
        "",
        "  Exit rules (bar sequence from bar 1; bar 0 = entry bar, not checked)",
        f"    Target                       {target_pct_original*100:.0f}%",
        f"    Stop                         {stop_pct_original*100:.0f}%",
        "    Ambiguity (target & stop same bar)  always counted as Stop Hit",
        "",
        "------------------------------------------------------------------------\n  REALISM SETTINGS (slippage, noise, commissions)\n------------------------------------------------------------------------\n",
        "  Slippage (worse fill on entry and exit)",
        f"    Base slippage                 {slippage_pct*100:.2f}% (entry +%, exit −%)",
        f"    Volatile-day slippage        {slippage_volatile_pct*100:.2f}% when day range >= {volatile_threshold*100:.0f}%",
        "",
        "  Extra fill noise (random adverse)",
        f"    Adverse fill noise            [0, {adverse_fill_noise_pct*100:.2f}%] on entry and exit (uniform)",
        "",
        "  Target miss (realism)",
        f"    Target-miss probability       {target_miss_pct*100:.0f}% of target hits → exit at close instead of target",
        "",
        "  Gap-through stop (stop slippage)",
        f"    Gap-through probability       {gap_through_stop_pct*100:.0f}% of stop hits → worse fill",
        f"    If first open < stop          fill = min(stop, first open)",
        f"    Else                          fill = stop × (1 − {gap_through_worse_pct*100:.2f}%)",
        "",
        "  Commissions (IBKR-style)",
        f"    Per share                     ${ibkr_per_share}",
        f"    Min per side                  ${ibkr_min_per_side:.2f}",
        f"    Max per side                  {ibkr_max_pct_of_trade*100:.2f}% of trade value",
        "",
        "  Reproducibility",
        f"    Random seed                   {realism_seed}",
        "",
        "------------------------------------------------------------------------\n  RESULTS SUMMARY\n------------------------------------------------------------------------\n",
        "------------------------------------------------------------------------\n  RESULTS SUMMARY\n------------------------------------------------------------------------\n",
        f"  Trading days with trades     {n_days}",
        f"  Total trades                 {n_trades}",
        f"  Initial capital              ${initial_capital:,.2f}",
        f"  Final capital                ${capital:,.2f}",
        f"  Total P&L                    ${total_pnl:,.2f}",
        f"  Return                       {ret_pct:.2f}%",
        f"  Avg daily P&L                ${avg_daily:,.2f}",
        f"  Best day                     {_dstr(best_date)}  P&L ${best_pnl:,.2f}",
        f"  Worst day                    {_dstr(worst_date)}  P&L ${worst_pnl:,.2f}",
        f"  Max drawdown                 ${max_dd:,.2f} ({max_dd_pct:.1f}%)",
        f"  Win rate                     {win_rate:.1f}% ({wins} wins / {losses} losses)\n",
        "------------------------------------------------------------------------\n  CLUSTERING & RISK (quick wins)\n------------------------------------------------------------------------\n",
        f"  Worst 5-day rolling P&L      ${worst_5d_val:,.2f}  (ending {worst_5d_end_date})",
        f"  Max stops in one day         {max_stops_one_day}",
        f"  Stops per day distribution   {stops_dist_str}",
        f"  Longest losing streak        {worst_run_count} trades  P&L ${worst_run_pnl:,.2f}",
        f"  Worst 10 consecutive trades  P&L ${worst_10_pnl:,.2f}  ({worst_10_pct:.1f}% of capital at start of run)",
        f"  Theoretical one-day loss     {theoretical_one_day_pct:.1f}% of capital (all positions stop, before slippage)\n",
        "------------------------------------------------------------------------\n  INTRADAY DRAWDOWN\n------------------------------------------------------------------------\n",
        f"  Max intraday drawdown        ${max_dd_intra:,.2f} ({max_dd_intra_pct:.1f}%)",
        f"  Max single-day intraday DD   ${max_single_day_intraday_dd:,.2f}  on {max_single_day_intraday_dd_date}\n",
    ]
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Report saved to '{report_path}'")

print(f"\nFinal capital: ${capital:,.2f}  (started ${initial_capital:,.2f})  Total P&L: ${capital - initial_capital:,.2f}")
