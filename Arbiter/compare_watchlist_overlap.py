#!/usr/bin/env python3
"""
Compare equity subscription lists:
  A) Fresh rescan for the prior session before --date (same as backtest for that day)
  B) data/watchlist_cache.txt (last list saved by live when rescan succeeded)

Prints overlap metrics. Does not prove causality; shows whether cached live list matches
current rescan logic for that calendar setup.

Usage:
  python compare_watchlist_overlap.py 2026-04-17
  python compare_watchlist_overlap.py --date 2026-04-17
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

import pytz

import config
from ib_connection import connect_ib, disconnect_ib
from metrics_cache import load_cached_watchlist
from universe_rescan import rescan_universe_for_day

EASTERN = pytz.timezone("America/New_York")


def _prior_trading_date_for_backtest_day(d: date) -> date:
    """Same calendar logic as backtest _prior_trading_day_close_et (previous weekday)."""
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def _load_sp500_candidates() -> list[str]:
    path = getattr(config, "SP500_TICKERS_FILE", os.path.join(config.BASE_DIR, "sp500_tickers.txt"))
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip().upper()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def subscription_subs(watchlist: list[str]) -> list[str]:
    """Match main/backtest + Launch: max_subs slice, ensure ETF, return ordered subs list."""
    etf = getattr(config, "ETF_SYMBOL", "SPY").upper()
    max_subs = int(getattr(config, "MAX_REALTIME_SUBSCRIPTIONS", 100))
    subs = list(watchlist[:max_subs])
    if etf not in subs:
        if len(subs) >= max_subs:
            subs = subs[:-1] + [etf]
        else:
            subs.append(etf)
    return subs


def _metrics(wl_a: set[str], wl_b: set[str]) -> dict:
    inter = wl_a & wl_b
    union = wl_a | wl_b
    jaccard = len(inter) / len(union) if union else 1.0
    pct_a = len(inter) / len(wl_a) if wl_a else 1.0
    pct_b = len(inter) / len(wl_b) if wl_b else 1.0
    return {
        "intersection": len(inter),
        "union": len(union),
        "only_a": sorted(wl_a - wl_b),
        "only_b": sorted(wl_b - wl_a),
        "jaccard_pct": jaccard * 100.0,
        "pct_of_a_matched": pct_a * 100.0,
        "pct_of_b_matched": pct_b * 100.0,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Compare rescan vs cached watchlist overlap")
    p.add_argument(
        "date",
        nargs="?",
        help="Session date YYYY-MM-DD (the day you backtest / trade)",
    )
    p.add_argument("--date", dest="date_kw", metavar="YYYY-MM-DD", default=None)
    args = p.parse_args()
    ds = args.date_kw or args.date
    if not ds:
        print("Need a date, e.g. python compare_watchlist_overlap.py 2026-04-17", file=sys.stderr)
        sys.exit(1)
    try:
        session_day = datetime.strptime(ds.strip(), "%Y-%m-%d").date()
    except ValueError:
        print("Invalid date; use YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    prior = _prior_trading_date_for_backtest_day(session_day)
    top_n = int(getattr(config, "WATCHLIST_TOP_N", 150))
    candidates = _load_sp500_candidates()

    watchlist_cache_path = getattr(config, "WATCHLIST_CACHE", os.path.join(config.DATA_DIR, "watchlist_cache.txt"))
    cache_raw = load_cached_watchlist()
    if cache_raw:
        cache_top = [s.upper() for s in cache_raw if s][:top_n]
        subs_cache = subscription_subs(cache_top)
        set_cache = set(subs_cache)
    else:
        cache_top = []
        subs_cache = []
        set_cache = set()
        print(f"No cached watchlist at {watchlist_cache_path} (or load failed).")

    print(f"Session day (backtest/live trading day): {session_day}")
    print(f"Prior session date for rescan (no lookahead): {prior}")
    print(f"WATCHLIST_TOP_N={top_n}, MAX_REALTIME_SUBSCRIPTIONS={getattr(config, 'MAX_REALTIME_SUBSCRIPTIONS', 100)}")
    print()

    ib = None
    try:
        print("Connecting to IB (needed if external screen insufficient)...")
        ib = connect_ib()
        wl_rescan, _ = rescan_universe_for_day(prior, ib, candidates, top_n, progress_callback=None)
    finally:
        if ib is not None:
            disconnect_ib(ib)

    if not wl_rescan:
        print("Rescan returned empty; cannot compare.", file=sys.stderr)
        sys.exit(1)

    subs_rescan = subscription_subs(wl_rescan)
    set_rescan = set(subs_rescan)

    print(f"Rescan watchlist length (top_n): {len(wl_rescan)}")
    print(f"Rescan subscription list ({len(subs_rescan)}): {', '.join(subs_rescan[:12])}{'...' if len(subs_rescan) > 12 else ''}")
    if subs_cache:
        print(f"Cache subscription list ({len(subs_cache)}): {', '.join(subs_cache[:12])}{'...' if len(subs_cache) > 12 else ''}")
    print()

    if not set_cache:
        print("No cache set to compare.")
        sys.exit(0)

    m = _metrics(set_rescan, set_cache)
    print("--- Overlap: fresh rescan vs watchlist_cache (live artifact) ---")
    print(f"Intersection size: {m['intersection']}")
    print(f"Union size:        {m['union']}")
    print(f"Jaccard similarity: {m['jaccard_pct']:.1f}%")
    print(f"% of rescan subs also in cache: {m['pct_of_a_matched']:.1f}%")
    print(f"% of cache subs also in rescan: {m['pct_of_b_matched']:.1f}%")
    if m["only_a"][:20]:
        print(f"Only in rescan (sample): {', '.join(m['only_a'][:20])}")
    if m["only_b"][:20]:
        print(f"Only in cache (sample): {', '.join(m['only_b'][:20])}")


if __name__ == "__main__":
    main()
