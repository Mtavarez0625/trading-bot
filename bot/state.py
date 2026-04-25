from __future__ import annotations

from collections import defaultdict
from datetime import date
from threading import Lock
from typing import Dict, Optional


class TradingState:
    """
    In-memory per-session state.

    Tracks:
    - Daily trade counts per symbol
    - Day-start equity (for daily-loss-stop calculations)

    Both reset automatically when the calendar date changes.
    Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._trade_date: date = date.today()
        self._trade_counts: Dict[str, int] = defaultdict(int)
        self._start_equity: Optional[float] = None

    def reset_if_new_day(self) -> bool:
        """
        If today's date differs from the stored date, reset all per-day state.
        Returns True if a reset occurred.
        """
        today = date.today()
        with self._lock:
            if today != self._trade_date:
                self._trade_counts.clear()
                self._start_equity = None
                self._trade_date = today
                return True
        return False

    # -----------------------------------------------------------------------
    # Trade counts
    # -----------------------------------------------------------------------

    def get_trade_count(self, symbol: str) -> int:
        with self._lock:
            return self._trade_counts[symbol.upper()]

    def increment_trade_count(self, symbol: str) -> int:
        """Increment and return the new count for `symbol`."""
        with self._lock:
            self._trade_counts[symbol.upper()] += 1
            return self._trade_counts[symbol.upper()]

    def all_counts(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._trade_counts)

    # -----------------------------------------------------------------------
    # Day-start equity  (used for daily loss stop)
    # -----------------------------------------------------------------------

    def set_start_equity(self, equity: float) -> None:
        """Record the day's starting equity — set only once per day."""
        with self._lock:
            if self._start_equity is None:
                self._start_equity = equity

    def get_start_equity(self) -> Optional[float]:
        with self._lock:
            return self._start_equity
