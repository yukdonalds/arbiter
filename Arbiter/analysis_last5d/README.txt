Arbiter analysis pack: last 5 Mon–Fri session dates ending on the latest Date in logs/trades.csv (market holidays not removed).
Trading days: 2026-04-20, 2026-04-21, 2026-04-22, 2026-04-23, 2026-04-24

session_rth_excerpt.log: only lines whose instant falls in RTH in America/New_York. Raw timestamps are local PC time, so a line can show e.g. 00:00 when that moment is 10:00 ET.

Included:
  logs/trades.csv, trade_outcomes.csv, signals.csv — rows for those dates only
  logs/daily_equity.csv, daily_regime.csv — same
  logs/session_rth_excerpt.log — session.log lines in RTH (US/Eastern 09:30–16:00),
     with Skipped/Confirmed lines subsampled 1 in 8 to limit size
  Reports/daily_report_*.txt for each day that exists
  data/daily_metrics_cache.csv (full snapshot, no date column)

session.log: read 150164 lines, kept 4901 (dropped: not in date set or not RTH in ET, ~114882).
