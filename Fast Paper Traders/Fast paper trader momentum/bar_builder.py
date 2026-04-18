# -----------------------------
# Fast Paper Trader – Bar builder from 5-sec bars (configurable BAR_MINUTES)
# -----------------------------
from collections import defaultdict
from datetime import datetime, time
import pytz

import config

EASTERN = pytz.timezone("America/New_York")
BAR_MINUTES = getattr(config, "BAR_MINUTES", 5)

def _bar_close_times(open_time: time):
    h, m = open_time.hour, open_time.minute
    while (h, m) < (16, 0):
        yield (h, m)
        m += BAR_MINUTES
        if m >= 60:
            m -= 60
            h += 1

def _next_bar_close(now_et: datetime) -> datetime:
    h, mi = now_et.hour, now_et.minute
    mi = ((mi // BAR_MINUTES) + 1) * BAR_MINUTES
    if mi >= 60:
        mi = 0
        h += 1
    return now_et.replace(hour=h, minute=mi, second=0, microsecond=0)

class BarBuilder:
    def __init__(self):
        self._current = defaultdict(lambda: {
            "open": None, "high": None, "low": None, "close": None, "volume": 0,
            "start_et": None
        })
        self._closed = defaultdict(list)

    def _is_bar_close_time(self, t_et: datetime) -> bool:
        return t_et.minute % BAR_MINUTES == 0 and t_et.second == 0

    def push(self, ticker: str, price: float, size: float, t_et: datetime) -> None:
        cur = self._current[ticker]
        if cur["open"] is None:
            cur["open"] = cur["high"] = cur["low"] = cur["close"] = price
            cur["volume"] = size
            cur["start_et"] = t_et
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
            cur["volume"] += size

    def lock_bar(self, ticker: str, close_time_et: datetime) -> dict | None:
        cur = self._current[ticker]
        if cur["open"] is None:
            return None
        bar = {
            "open": cur["open"], "high": cur["high"], "low": cur["low"],
            "close": cur["close"], "volume": cur["volume"], "time_et": close_time_et
        }
        self._closed[ticker].append(bar)
        self._current[ticker] = {
            "open": None, "high": None, "low": None, "close": None, "volume": 0,
            "start_et": None
        }
        return bar

    def get_current_bar(self, ticker: str) -> dict | None:
        cur = self._current[ticker]
        if cur["open"] is None:
            return None
        return dict(cur)

    def get_latest_closed(self, ticker: str) -> dict | None:
        L = self._closed[ticker]
        return L[-1] if L else None

    def get_all_closed(self, ticker: str) -> list:
        return list(self._closed[ticker])
