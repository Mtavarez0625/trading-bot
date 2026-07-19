"""
Tests for broker-fill reconciliation:

- main._find_broker_closing_fill()  — shared closed-order lookup, never fabricates a price
- main._reconcile_journal_state()   — startup reconciliation using real broker fills
- main._poll_order_fill()           — bounded polling for entry-fill confirmation

None of these tests place real or paper orders — every Alpaca HTTP call is mocked.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import journal
import main


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


@pytest.fixture
def journal_db(tmp_path, monkeypatch):
    db_path = tmp_path / "reconcile_test.db"
    monkeypatch.setattr(journal, "DB_PATH", db_path)
    journal.init_db()
    return db_path


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


# ── _find_broker_closing_fill ──────────────────────────────────────────────────

class TestFindBrokerClosingFill:
    def test_returns_none_on_non_200(self, monkeypatch):
        monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(status_code=500))
        assert main._find_broker_closing_fill("RIOT") is None

    def test_returns_none_when_no_filled_sells(self, monkeypatch):
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload=[{"status": "canceled", "side": "sell"}]),
        )
        assert main._find_broker_closing_fill("RIOT") is None

    def test_returns_none_when_fill_price_missing(self, monkeypatch):
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload=[
                {"status": "filled", "side": "sell", "filled_avg_price": None, "type": "market"}
            ]),
        )
        assert main._find_broker_closing_fill("RIOT") is None

    def test_classifies_stop_loss_hit(self, monkeypatch):
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload=[
                {"status": "filled", "side": "sell", "filled_avg_price": "22.50", "type": "stop"}
            ]),
        )
        fill = main._find_broker_closing_fill("RIOT", stop_price=22.53, tp_price=23.91)
        assert fill == {"exit_price": 22.50, "exit_reason": "stop_loss_hit", "order_id": None}

    def test_classifies_take_profit_hit(self, monkeypatch):
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload=[
                {"status": "filled", "side": "sell", "filled_avg_price": "23.95", "type": "limit"}
            ]),
        )
        fill = main._find_broker_closing_fill("RIOT", stop_price=22.53, tp_price=23.91)
        assert fill["exit_reason"] == "take_profit_hit"
        assert fill["exit_price"] == 23.95

    def test_filters_fills_before_after_iso(self, monkeypatch):
        # An old fill from a previous, unrelated position should not be matched.
        old_fill = {
            "status": "filled", "side": "sell", "filled_avg_price": "99.00",
            "type": "market", "filled_at": _iso(minutes_ago=600),
        }
        monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(payload=[old_fill]))
        fill = main._find_broker_closing_fill("RIOT", after_iso=_iso(minutes_ago=5))
        assert fill is None

    def test_network_error_returns_none(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("boom")
        monkeypatch.setattr(main.requests, "get", _raise)
        assert main._find_broker_closing_fill("RIOT") is None


# ── _reconcile_journal_state ───────────────────────────────────────────────────

class TestReconcileJournalState:
    def test_dry_run_clears_with_suspect_zero_exit(self, journal_db, monkeypatch):
        monkeypatch.setattr(main, "DRY_RUN", True)
        journal.open_paper_trade("RIOT", {
            "entry_timestamp": _iso(60), "entry_price": 22.99, "stop_price": 22.53,
            "take_profit_price": 23.91, "qty": 8,
        })
        # No Alpaca positions call should matter in dry-run, but stub it anyway.
        monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(payload=[]))

        cleared = main._reconcile_journal_state()
        assert cleared == ["RIOT"]

        trades = journal.query_recent_trades(limit=5)
        row = trades[0]
        assert row["exit_reason"] == "reconcile_stale"
        assert row["exit_price"] == 0.0
        assert row["data_quality_status"] == "suspect_zero_exit"
        # Fabricated zero-price row must never leak into performance stats.
        perf = journal.query_performance_summary()
        assert perf.get("eligible_trade_count", 0) == 0

    def test_live_paper_uses_confirmed_broker_fill(self, journal_db, monkeypatch):
        monkeypatch.setattr(main, "DRY_RUN", False)
        journal.open_paper_trade("BAC", {
            "entry_timestamp": _iso(120), "entry_price": 52.34, "stop_price": 51.29,
            "take_profit_price": 54.43, "qty": 3,
        })

        def _fake_get(url, headers=None, params=None, timeout=None):
            if "positions" in url:
                return _FakeResponse(payload=[])  # Alpaca no longer holds BAC
            return _FakeResponse(payload=[{
                "status": "filled", "side": "sell", "filled_avg_price": "51.20",
                "type": "stop", "filled_at": _iso(minutes_ago=1),
            }])

        monkeypatch.setattr(main.requests, "get", _fake_get)
        cleared = main._reconcile_journal_state()
        assert cleared == ["BAC"]

        row = journal.query_recent_trades(limit=5)[0]
        assert row["exit_price"] == pytest.approx(51.20)
        assert row["exit_reason"] == "reconciled_stop_loss_hit"
        assert row["data_quality_status"] == "verified"
        perf = journal.query_performance_summary()
        assert perf.get("eligible_trade_count", 0) == 1

    def test_live_paper_no_fill_found_marks_unresolved_without_fabricating_price(
        self, journal_db, monkeypatch
    ):
        monkeypatch.setattr(main, "DRY_RUN", False)
        journal.open_paper_trade("XYZ", {
            "entry_timestamp": _iso(90), "entry_price": 10.0, "stop_price": 9.5,
            "take_profit_price": 11.0, "qty": 5,
        })

        def _fake_get(url, headers=None, params=None, timeout=None):
            return _FakeResponse(payload=[])  # nothing in Alpaca at all

        monkeypatch.setattr(main.requests, "get", _fake_get)
        cleared = main._reconcile_journal_state()
        assert cleared == ["XYZ"]

        row = journal.query_recent_trades(limit=5)[0]
        assert row["exit_price"] is None
        assert row["exit_reason"] == "unresolved_reconciliation"
        assert row["data_quality_status"] == "unresolved_reconciliation"
        assert row["realized_pnl"] is None
        # Never left dangling open, and never counted as a real result.
        assert journal.get_open_paper_positions() == []
        perf = journal.query_performance_summary()
        assert perf.get("eligible_trade_count", 0) == 0

    def test_ids_4_and_5_style_rows_are_never_produced_by_a_second_run(
        self, journal_db, monkeypatch
    ):
        """Reconciliation must be safe to run repeatedly without duplicating or mutating."""
        monkeypatch.setattr(main, "DRY_RUN", False)
        journal.open_paper_trade("RIOT", {
            "entry_timestamp": _iso(90), "entry_price": 22.99, "stop_price": 22.53,
            "take_profit_price": 23.91, "qty": 8,
        })
        monkeypatch.setattr(main.requests, "get", lambda *a, **k: _FakeResponse(payload=[]))

        first = main._reconcile_journal_state()
        second = main._reconcile_journal_state()
        assert first == ["RIOT"]
        assert second == []  # nothing open left to reconcile — idempotent
        assert len(journal.query_recent_trades(limit=10)) == 1


# ── _poll_order_fill ────────────────────────────────────────────────────────────

class TestPollOrderFill:
    def test_immediate_fill(self, monkeypatch):
        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload={"id": "o1", "status": "filled", "filled_avg_price": "10.05"}),
        )
        result = main._poll_order_fill("o1")
        assert result["status"] == "filled"
        assert result["filled_avg_price"] == "10.05"

    def test_delayed_fill_within_budget(self, monkeypatch):
        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        responses = iter([
            _FakeResponse(payload={"id": "o1", "status": "accepted"}),
            _FakeResponse(payload={"id": "o1", "status": "pending_new"}),
            _FakeResponse(payload={"id": "o1", "status": "filled", "filled_avg_price": "10.10"}),
        ])
        monkeypatch.setattr(main.requests, "get", lambda *a, **k: next(responses))
        result = main._poll_order_fill("o1", max_attempts=5, delay_sec=0)
        assert result["status"] == "filled"

    def test_rejection_is_terminal(self, monkeypatch):
        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload={"id": "o1", "status": "rejected"}),
        )
        result = main._poll_order_fill("o1", max_attempts=5, delay_sec=0)
        assert result["status"] == "rejected"

    def test_timeout_returns_last_known_non_terminal_state(self, monkeypatch):
        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload={"id": "o1", "status": "accepted"}),
        )
        result = main._poll_order_fill("o1", max_attempts=3, delay_sec=0)
        assert result["status"] == "accepted"  # never became terminal — caller must not fabricate a fill

    def test_never_loops_more_than_max_attempts(self, monkeypatch):
        calls = {"n": 0}

        def _fake_get(*a, **k):
            calls["n"] += 1
            return _FakeResponse(payload={"id": "o1", "status": "accepted"})

        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        monkeypatch.setattr(main.requests, "get", _fake_get)
        main._poll_order_fill("o1", max_attempts=4, delay_sec=0)
        assert calls["n"] == 4

    def test_missing_filled_avg_price_on_filled_order(self, monkeypatch):
        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            main.requests, "get",
            lambda *a, **k: _FakeResponse(payload={"id": "o1", "status": "filled", "filled_avg_price": None}),
        )
        result = main._poll_order_fill("o1")
        assert result["status"] == "filled"
        assert result.get("filled_avg_price") is None  # caller must fall back, not crash
