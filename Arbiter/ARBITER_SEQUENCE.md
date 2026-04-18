# Arbiter - Sequence of Events (Start to Finish)

- **Startup**
  - Load ticker universe from `sp500_tickers.txt`.
  - Connect to IBKR, detect account, read current net liquidation.
  - Initialize logs (`trades`, `equity`, `signals`, `trade_outcomes`, `daily_regime`).

- **Selection Method (Universe + Watchlist)**
  - If `USE_FIXED_UNIVERSE=False`: rescan prior trading session and build top ranked watchlist.
  - Otherwise: use cached watchlist/metrics first; if stale/missing, rebuild via external screen or IB parallel scan.
  - Keep top `WATCHLIST_TOP_N` symbols with daily metrics (`avg_vol_20`, `atr_pct`, `prev_close`, etc.).
  - Subscribe to 5-second real-time bars for watchlist + ETF bias symbol (usually SPY).

- **Bias + Signal Preparation**
  - Update ETF market bias on interval (`BIAS_REFRESH_INTERVAL`).
  - Build intrabar and bar-close candles with `BarBuilder`.
  - Calculate signal inputs (pct change, time-adjusted relative volume, ATR%, ADX, VWAP distance).
  - Run `check_v26_bar_side` for LONG/SHORT candidates.
  - Reject candidates near support/resistance zone.
  - Queue candidates for confirmation with ATR-based confirmation level.

- **Confirmation Stage**
  - Confirm on subsequent closed bars (up to 2 bars).
  - Fast-track confirmation allowed when ADX/relative-volume is strong.
  - Confirmed signals move to ranked execution list.

- **Trade Entry**
  - Rank/cap signals (`rank_and_cap`) and apply risk/time gates:
    - market-open timing, no new entries after cutoff, max positions, daily loss guard.
  - Apply soft SPY filter (counter-trend trades are size-reduced, not hard blocked).
  - Size trade from available capital and bias/quality multipliers.
  - Place marketable-limit entry order and track as pending.
  - Cancel unfilled entry after `ENTRY_ORDER_TIMEOUT_SECONDS`.

- **Post-Fill Setup**
  - On fill event, place partial + runner exits:
    - TP1 (partial take-profit),
    - runner cap target,
    - stop-loss.
  - Store entry metadata (signal price, slippage, score, rel_vol, ATR, rank).

- **Trade Management**
  - Continuously update MFE/MAE and trailing stop state.
  - Move to secure/breakeven stop after threshold MFE.
  - Activate trailing stop after higher MFE threshold.
  - Apply tighter protection logic after late-day cutoff when in profit.

- **Trade Exit**
  - Exit by TP1, runner target, stop, or EOD close.
  - Log each exit with PnL, exit reason, MFE/MAE, and slippage fields.
  - Update daily win/loss counters and total PnL.

- **Session End**
  - Force-close remaining positions near configured close time.
  - Cancel real-time streams and disconnect from IBKR.
  - Write end-of-day equity/regime rows.
  - Generate daily report in `Reports/`.
