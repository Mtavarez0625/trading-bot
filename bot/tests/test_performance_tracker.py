from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from performance_tracker import PerformanceTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracker(tmp_path: Path) -> PerformanceTracker:
    return PerformanceTracker(summary_path=tmp_path / "session_summary.json")


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_signals_evaluated_zero(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert t.signals_evaluated == 0

    def test_trades_attempted_zero(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert t.trades_attempted == 0

    def test_trades_executed_zero(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert t.trades_executed == 0

    def test_trades_skipped_zero(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert t.trades_skipped == 0

    def test_skip_reasons_empty(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert len(t.skip_reasons) == 0


# ---------------------------------------------------------------------------
# record_signal
# ---------------------------------------------------------------------------


class TestRecordSignal:
    def test_increments_signals_evaluated(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_signal("AAPL")
        t.record_signal("MSFT")
        assert t.signals_evaluated == 2

    def test_does_not_affect_trades(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_signal("AAPL")
        assert t.trades_attempted == 0
        assert t.trades_executed == 0


# ---------------------------------------------------------------------------
# record_attempt / record_execution
# ---------------------------------------------------------------------------


class TestRecordAttemptExecution:
    def test_attempt_increments_trades_attempted(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        assert t.trades_attempted == 1

    def test_attempt_increments_symbol_attempts(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        t.record_attempt("AAPL")
        assert t.symbol_attempts("AAPL") == 2

    def test_execution_increments_trades_executed(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        t.record_execution("AAPL")
        assert t.trades_executed == 1

    def test_execution_increments_symbol_executions(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        t.record_execution("AAPL")
        assert t.symbol_executions("AAPL") == 1

    def test_symbol_attempts_case_insensitive(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("aapl")
        assert t.symbol_attempts("AAPL") == 1

    def test_multiple_symbols_tracked_independently(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        t.record_attempt("MSFT")
        t.record_attempt("MSFT")
        assert t.symbol_attempts("AAPL") == 1
        assert t.symbol_attempts("MSFT") == 2

    def test_zero_attempts_for_unseen_symbol(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert t.symbol_attempts("NVDA") == 0
        assert t.symbol_executions("NVDA") == 0


# ---------------------------------------------------------------------------
# trades_skipped property
# ---------------------------------------------------------------------------


class TestTradesSkipped:
    def test_skipped_equals_attempted_minus_executed(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        t.record_attempt("MSFT")
        t.record_execution("AAPL")
        assert t.trades_skipped == 1

    def test_all_executed_zero_skipped(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        t.record_execution("AAPL")
        assert t.trades_skipped == 0

    def test_no_activity_zero_skipped(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert t.trades_skipped == 0


# ---------------------------------------------------------------------------
# record_skip
# ---------------------------------------------------------------------------


class TestRecordSkip:
    def test_skip_increments_reason_counter(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_skip("AAPL", "open_position_exists")
        t.record_skip("MSFT", "open_position_exists")
        assert t.skip_reasons["open_position_exists"] == 2

    def test_skip_recorded_per_symbol(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_skip("AAPL", "open_position_exists")
        t.record_skip("AAPL", "daily_limit_reached")
        summary = t.symbol_skip_summary("AAPL")
        assert summary["open_position_exists"] == 1
        assert summary["daily_limit_reached"] == 1

    def test_skip_reason_truncated_at_80_chars(self, tmp_path):
        t = _make_tracker(tmp_path)
        long_reason = "x" * 200
        t.record_skip("AAPL", long_reason)
        stored = list(t.skip_reasons.keys())[0]
        assert len(stored) <= 80

    def test_symbol_skip_summary_empty_for_unseen(self, tmp_path):
        t = _make_tracker(tmp_path)
        assert t.symbol_skip_summary("NVDA") == {}

    def test_skip_case_insensitive_symbol(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_skip("aapl", "weak_signal")
        assert t.symbol_skip_summary("AAPL") == {"weak_signal": 1}

    def test_all_skipped_symbols_returns_all(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_skip("AAPL", "open_position_exists")
        t.record_skip("MSFT", "no_bar_data")
        result = t.all_skipped_symbols()
        assert "AAPL" in result
        assert "MSFT" in result


# ---------------------------------------------------------------------------
# save() — JSON persistence
# ---------------------------------------------------------------------------


class TestSave:
    def test_creates_json_file(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.save()
        path = tmp_path / "session_summary.json"
        assert path.exists()

    def test_json_is_valid(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_signal("AAPL")
        t.record_attempt("AAPL")
        t.record_execution("AAPL")
        t.save()
        data = json.loads((tmp_path / "session_summary.json").read_text())
        assert isinstance(data, dict)

    def test_json_has_required_keys(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.save()
        data = json.loads((tmp_path / "session_summary.json").read_text())
        for key in (
            "session_start", "session_end",
            "signals_evaluated", "trades_attempted",
            "trades_executed", "trades_skipped",
            "skip_reasons", "per_symbol",
        ):
            assert key in data, f"Missing key: {key}"

    def test_json_counts_match_tracker_state(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_signal("AAPL")
        t.record_attempt("AAPL")
        t.record_execution("AAPL")
        t.record_skip("MSFT", "weak_signal")
        t.save()
        data = json.loads((tmp_path / "session_summary.json").read_text())
        assert data["signals_evaluated"] == 1
        assert data["trades_attempted"] == 1
        assert data["trades_executed"] == 1
        assert data["skip_reasons"]["weak_signal"] == 1

    def test_json_per_symbol_structure(self, tmp_path):
        t = _make_tracker(tmp_path)
        t.record_attempt("AAPL")
        t.record_execution("AAPL")
        t.save()
        data = json.loads((tmp_path / "session_summary.json").read_text())
        assert "AAPL" in data["per_symbol"]
        sym = data["per_symbol"]["AAPL"]
        assert sym["attempts"] == 1
        assert sym["executions"] == 1

    def test_save_does_not_raise_on_bad_path(self, tmp_path):
        t = PerformanceTracker(summary_path=Path("/nonexistent/path/summary.json"))
        t.save()  # should log error, not raise
