# -----------------------------
# Shared universe rescan (live Run Me + backtest, non-fixed universe)
# -----------------------------
from datetime import date, datetime, timedelta

import pytz

import config
from external_screen import build_watchlist_external_for_date
from parallel_fetch import build_watchlist_parallel_for_date

EASTERN = pytz.timezone("America/New_York")


def prior_trading_session_date(now_et: datetime) -> date:
    """
    Calendar date of the last completed US equity session before the current session,
    aligned with backtest daily-metrics anchoring (no lookahead).
    """
    d = now_et.date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    prior = d - timedelta(days=1)
    while prior.weekday() >= 5:
        prior -= timedelta(days=1)
    return prior


def _end_dt_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d %H:%M:%S US/Eastern")


def rescan_universe_for_day(
    prior_trading_date: date,
    ib,
    candidates: list[str],
    top_n: int,
    progress_callback=None,
) -> tuple[list[str], dict]:
    """
    Build watchlist using prior trading day's data only (no lookahead).
    Tries external screen (yfinance) first; falls back to IB parallel if needed.
    Used by live Run Me FPLS at startup and backtest_fpls_yesterday per-day loop.
    """
    use_external = getattr(config, "USE_EXTERNAL_SCREEN", True)
    if use_external:
        result = build_watchlist_external_for_date(
            prior_trading_date, top_n=top_n, use_backtest_volume_min=False
        )
        if result:
            watchlist, metrics = result
            if len(watchlist) >= 10:
                return watchlist, metrics
    metrics_end_dt = EASTERN.localize(
        datetime.combine(
            prior_trading_date,
            datetime.min.time().replace(hour=16, minute=0, second=0, microsecond=0),
        )
    )
    metrics_end_dt_str = _end_dt_str(metrics_end_dt)
    return build_watchlist_parallel_for_date(
        ib,
        candidates,
        metrics_end_dt_str,
        top_n=top_n,
        use_backtest_volume_min=False,
        progress_callback=progress_callback,
    )
