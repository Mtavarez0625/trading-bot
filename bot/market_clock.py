from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from logger import get_logger

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current datetime in US/Eastern. Isolated here so tests can monkeypatch it."""
    return datetime.now(tz=ET)


def is_market_open(trading_client) -> bool:
    """
    Ask Alpaca whether the market is currently open.
    Returns False on any API or network failure.
    """
    try:
        clock = trading_client.get_clock()
        return bool(clock.is_open)
    except Exception as exc:
        log.warning("Failed to fetch market clock: %s", exc)
        return False


def is_within_entry_window(window_start: time, window_end: time) -> bool:
    """
    Return True if the current ET time falls within [window_start, window_end] inclusive.
    """
    current = now_et().time()
    return window_start <= current <= window_end


def market_open_time() -> datetime:
    """Return today's market open (9:30 AM ET). Utility for logging."""
    today = now_et().date()
    return datetime(today.year, today.month, today.day, 9, 30, tzinfo=ET)
