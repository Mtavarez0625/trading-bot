from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

_DEFAULT_SUMMARY_PATH = Path(__file__).parent / "session_summary.json"


class PerformanceTracker:
    """
    Tracks signals, trade attempts, executions, and skip reasons for the
    current session. Replaces the inline SessionStats in main.py.

    Persists a summary to session_summary.json via save().

    Design:
    - record_signal(symbol)    — called after evaluate_signal() runs
    - record_attempt(symbol)   — called when signal fires and order is queued
    - record_execution(symbol) — called after a successful order submission
    - record_skip(symbol, reason) — called on any early-exit path

    All methods are safe to call from a single thread (no locking needed
    for the single-threaded bot loop).
    """

    def __init__(self, *, summary_path: Path = _DEFAULT_SUMMARY_PATH) -> None:
        self._summary_path = summary_path
        self._session_start: datetime = datetime.now(timezone.utc)

        # Aggregate counters
        self.signals_evaluated: int = 0
        self.trades_attempted: int = 0
        self.trades_executed: int = 0
        self.skip_reasons: Counter = Counter()

        # Per-symbol counters
        self._symbol_attempts: Counter = Counter()
        self._symbol_executions: Counter = Counter()
        self._symbol_skips: Dict[str, Counter] = defaultdict(Counter)

    # -----------------------------------------------------------------------
    # Recording
    # -----------------------------------------------------------------------

    def record_signal(self, symbol: str) -> None:
        """Record that a signal was evaluated for this symbol."""
        self.signals_evaluated += 1

    def record_attempt(self, symbol: str) -> None:
        """Record that a trade was attempted (signal fired, order queued)."""
        self.trades_attempted += 1
        self._symbol_attempts[symbol.upper()] += 1

    def record_execution(self, symbol: str) -> None:
        """Record that an order was submitted successfully."""
        self.trades_executed += 1
        self._symbol_executions[symbol.upper()] += 1

    def record_skip(self, symbol: str, reason: str) -> None:
        """Record a skip for any reason at any point in symbol evaluation."""
        reason = reason[:80]
        self.skip_reasons[reason] += 1
        self._symbol_skips[symbol.upper()][reason] += 1

    # -----------------------------------------------------------------------
    # Derived properties
    # -----------------------------------------------------------------------

    @property
    def trades_skipped(self) -> int:
        return self.trades_attempted - self.trades_executed

    def symbol_attempts(self, symbol: str) -> int:
        return self._symbol_attempts[symbol.upper()]

    def symbol_executions(self, symbol: str) -> int:
        return self._symbol_executions[symbol.upper()]

    def symbol_skip_summary(self, symbol: str) -> Dict[str, int]:
        """Return {reason: count} for skips on this symbol."""
        return dict(self._symbol_skips.get(symbol.upper(), {}))

    def all_skipped_symbols(self) -> Dict[str, Dict[str, int]]:
        """Return per-symbol skip breakdown for all symbols that had skips."""
        return {sym: dict(ctr) for sym, ctr in self._symbol_skips.items()}

    # -----------------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------------

    def log_summary(self, logger: Optional[logging.Logger] = None) -> None:
        _log = logger or log
        _log.info("=" * 60)
        _log.info("SESSION SUMMARY")
        _log.info(
            "  Session start     : %s",
            self._session_start.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        _log.info("  Signals evaluated : %d", self.signals_evaluated)
        _log.info("  Trades attempted  : %d", self.trades_attempted)
        _log.info("  Trades executed   : %d", self.trades_executed)
        _log.info("  Trades skipped    : %d", self.trades_skipped)

        if self.skip_reasons:
            _log.info("  Skip reasons:")
            for reason, count in self.skip_reasons.most_common():
                _log.info("    %-45s %d×", reason, count)

        all_syms = sorted(
            set(self._symbol_attempts)
            | set(self._symbol_executions)
            | set(self._symbol_skips)
        )
        if all_syms:
            _log.info("  Per-symbol results:")
            for sym in all_syms:
                _log.info(
                    "    %-8s  attempted=%d  executed=%d  skipped=%d",
                    sym,
                    self._symbol_attempts[sym],
                    self._symbol_executions[sym],
                    sum(self._symbol_skips.get(sym, Counter()).values()),
                )
        _log.info("=" * 60)

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def save(self) -> None:
        """Write session_summary.json. Logs errors but never raises."""
        try:
            all_syms = sorted(
                set(self._symbol_attempts)
                | set(self._symbol_executions)
                | set(self._symbol_skips)
            )
            summary = {
                "session_start": self._session_start.isoformat(),
                "session_end": datetime.now(timezone.utc).isoformat(),
                "signals_evaluated": self.signals_evaluated,
                "trades_attempted": self.trades_attempted,
                "trades_executed": self.trades_executed,
                "trades_skipped": self.trades_skipped,
                "skip_reasons": dict(self.skip_reasons.most_common()),
                "per_symbol": {
                    sym: {
                        "attempts": self._symbol_attempts[sym],
                        "executions": self._symbol_executions[sym],
                        "skips": dict(self._symbol_skips.get(sym, Counter())),
                    }
                    for sym in all_syms
                },
            }
            self._summary_path.write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
            log.info("Session summary saved → %s", self._summary_path)
        except Exception as exc:
            log.error("Failed to save session summary: %s", exc)
