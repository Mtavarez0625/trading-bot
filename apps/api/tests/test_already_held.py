"""
Tests for:
- BUY-signal already-held diagnostic: blocked_by="already_held" (renamed from the
  previous "duplicate_position_guard"), for both the Alpaca-position and
  journal-only cases. Confirms this is a label-only change — no order is placed,
  no position is closed or added to, and an already-held symbol can never be
  miscounted as a submitted entry in blocker analytics.
- Confirmed entry-fill capture on a real BUY execution: the journal records the
  broker's confirmed filled_avg_price, not just the decision-time signal price,
  and rejected/canceled orders never produce a phantom journal entry.

Every Alpaca HTTP call and the scoring/signal pipeline is mocked — no real or
paper orders are placed by these tests.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import journal
import main


BUY_SIGNAL = {
    "signal": "BUY",
    "signal_reason": "strong trend breakout",
    "decision_summary": "BUY: strong trend",
    "close": 10.00,
    "entry_tier": "strong",
    "trend_strength": 0.05,
    "rsi": 60,
    "macd_line": 0.5,
    "macd_signal_line": 0.3,
    "macd_histogram": 0.2,
    "macd_histogram_rising": True,
    "current_volume": 2_000_000,
    "vol_sma_20": 1_000_000,
    "volume_confirmed": True,
    "breakout_confirmed": True,
    "intraday_confirmed": True,
    "spy_bullish": True,
    "spy_regime": "bullish",
}


@pytest.fixture
def journal_db(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "already_held_test.db")
    journal.init_db()


@pytest.fixture
def common_mocks(monkeypatch, journal_db):
    """Minimal mocks so execute_trade() reaches the BUY gate logic without any network calls."""
    monkeypatch.setattr(main, "is_market_open", lambda: True)
    monkeypatch.setattr(main, "get_signal", lambda symbol: dict(BUY_SIGNAL))
    monkeypatch.setattr(main, "ensure_protection_for_position", lambda *a, **k: None)


class TestAlreadyHeldBlocker:
    def test_alpaca_position_already_held_tagged_already_held(self, common_mocks, monkeypatch):
        monkeypatch.setattr(main, "get_position", lambda symbol: {"qty": 10, "avg_entry_price": 9.5})
        monkeypatch.setattr(journal, "has_open_paper_trade", lambda symbol: False)

        result = main.execute_trade("RIOT")

        assert result["blocked_by"] == "already_held"
        assert "already_held" in result["decision_summary"]
        assert result["signal"] == "BUY"
        # No new journal entry was created and no position was touched.
        assert journal.get_open_paper_positions() == []

    def test_journal_only_already_held_tagged_already_held(self, common_mocks, monkeypatch):
        monkeypatch.setattr(main, "get_position", lambda symbol: {"qty": 0, "avg_entry_price": 0.0})
        monkeypatch.setattr(journal, "has_open_paper_trade", lambda symbol: True)

        result = main.execute_trade("RIOT")

        assert result["blocked_by"] == "already_held"
        assert "journal_entry" in result["decision_summary"]

    def test_already_held_never_miscounted_as_submitted_entry(self, common_mocks, monkeypatch):
        monkeypatch.setattr(main, "get_position", lambda symbol: {"qty": 10, "avg_entry_price": 9.5})
        monkeypatch.setattr(journal, "has_open_paper_trade", lambda symbol: False)

        result = main.execute_trade("RIOT")

        # A blocked-by result must not carry the markers of a real new entry.
        assert result.get("new_entry_opened", False) is False
        assert not any(a.get("step") == "open_long_bracket" for a in result.get("actions", []))

    def test_no_longer_reports_old_duplicate_position_guard_label(self, common_mocks, monkeypatch):
        monkeypatch.setattr(main, "get_position", lambda symbol: {"qty": 10, "avg_entry_price": 9.5})
        monkeypatch.setattr(journal, "has_open_paper_trade", lambda symbol: False)

        result = main.execute_trade("RIOT")
        assert result["blocked_by"] != "duplicate_position_guard"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _order_status_router(order_payload: dict):
    """
    Route requests.get() calls by URL: account-equity lookups get a funded paper
    account, bars/quote lookups get an innocuous empty response (so they don't trip
    the symbol-error-cooldown / observe-only-mode circuit breakers), and everything
    else (order-status polls) gets the given terminal/non-terminal payload.
    """
    def _get(url, headers=None, params=None, timeout=None):
        if "/v2/account" in url:
            return _FakeResponse({"equity": "1000.00"})
        if "/bars" in url or "/quotes" in url or "/v2/stocks" in url:
            return _FakeResponse({"bars": [], "quotes": []})
        if url.rstrip("/").endswith("/v2/orders"):
            return _FakeResponse([])  # open-orders listing — nothing to cancel
        return _FakeResponse(order_payload)  # /v2/orders/{id} status poll
    return _get


class TestConfirmedEntryFill:
    """execute_trade's BUY path, focused on Phase 5 fill-confirmation behavior."""

    def _common_buy_mocks(self, monkeypatch):
        monkeypatch.setattr(main, "get_position", lambda symbol: {"qty": 0, "avg_entry_price": 0.0})
        monkeypatch.setattr(journal, "has_open_paper_trade", lambda symbol: False)
        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        monkeypatch.setattr(main, "DRY_RUN", False)
        monkeypatch.setattr(main, "_is_past_last_entry_time", lambda: False)
        monkeypatch.setattr(main, "_get_or_build_opening_range", lambda symbol: {"formed": True, "high": 5.0, "low": 4.0})
        main._symbol_error_counts.clear()
        main._symbol_error_cooldown.clear()
        main._session_symbol_trade_count.clear()
        main._last_trade_time.clear()
        monkeypatch.setattr(main, "_api_failure_count", 0)
        monkeypatch.setattr(main, "_observe_only_mode", False)

    def test_confirmed_fill_price_overrides_decision_price(self, common_mocks, monkeypatch):
        self._common_buy_mocks(monkeypatch)
        monkeypatch.setattr(main, "_submit_order", lambda order: {"id": "order-1", "status": "accepted"})
        monkeypatch.setattr(
            main.requests, "get",
            _order_status_router(
                {"id": "order-1", "status": "filled", "filled_avg_price": "10.07", "filled_qty": "9"}
            ),
        )

        result = main.execute_trade("TST1")

        assert result["entry_price"] == pytest.approx(10.07)  # not the decision-time 10.00
        open_positions = journal.get_open_paper_positions()
        assert len(open_positions) == 1
        assert open_positions[0]["entry_price"] == pytest.approx(10.07)

    def test_rejected_order_does_not_create_phantom_journal_entry(self, common_mocks, monkeypatch):
        self._common_buy_mocks(monkeypatch)
        monkeypatch.setattr(main, "_submit_order", lambda order: {"id": "order-2", "status": "accepted"})
        monkeypatch.setattr(
            main.requests, "get", _order_status_router({"id": "order-2", "status": "rejected"})
        )

        result = main.execute_trade("TST2")

        assert result["blocked_by"] == "order_rejected"
        assert journal.get_open_paper_positions() == []

    def test_canceled_order_does_not_create_phantom_journal_entry(self, common_mocks, monkeypatch):
        self._common_buy_mocks(monkeypatch)
        monkeypatch.setattr(main, "_submit_order", lambda order: {"id": "order-3", "status": "accepted"})
        monkeypatch.setattr(
            main.requests, "get", _order_status_router({"id": "order-3", "status": "canceled"})
        )

        result = main.execute_trade("TST3")

        assert result["blocked_by"] == "order_canceled"
        assert journal.get_open_paper_positions() == []

    def test_pending_after_timeout_uses_decision_price_but_flags_pending(self, common_mocks, monkeypatch):
        self._common_buy_mocks(monkeypatch)
        monkeypatch.setattr(main, "_submit_order", lambda order: {"id": "order-4", "status": "accepted"})
        monkeypatch.setattr(
            main.requests, "get", _order_status_router({"id": "order-4", "status": "pending_new"})
        )

        result = main.execute_trade("TST4")

        assert result["entry_price"] == pytest.approx(10.00)  # decision-time estimate, not fabricated fill
        row = journal.query_recent_trades(limit=1)[0]
        assert row["data_quality_status"] == "pending_entry_fill"
        # Unconfirmed entries must be excluded from performance stats once closed.
        journal.close_paper_trade(row["symbol"], 10.5, "take_profit_hit")
        perf = journal.query_performance_summary()
        assert perf["eligible_trade_count"] == 0

    def test_missing_order_id_flags_pending_without_crashing(self, common_mocks, monkeypatch):
        self._common_buy_mocks(monkeypatch)
        monkeypatch.setattr(main, "_submit_order", lambda order: {"status": "accepted"})  # no "id"
        monkeypatch.setattr(main.requests, "get", _order_status_router({}))

        result = main.execute_trade("TST5")

        assert result["entry_price"] == pytest.approx(10.00)
        row = journal.query_recent_trades(limit=1)[0]
        assert row["data_quality_status"] == "pending_entry_fill"

    def test_dry_run_never_polls_broker(self, common_mocks, monkeypatch):
        monkeypatch.setattr(main, "get_position", lambda symbol: {"qty": 0, "avg_entry_price": 0.0})
        monkeypatch.setattr(journal, "has_open_paper_trade", lambda symbol: False)
        monkeypatch.setattr(main, "_is_past_last_entry_time", lambda: False)
        monkeypatch.setattr(main, "_get_or_build_opening_range", lambda symbol: {"formed": True, "high": 5.0, "low": 4.0})
        main._symbol_error_counts.clear()
        main._symbol_error_cooldown.clear()
        main._session_symbol_trade_count.clear()
        main._last_trade_time.clear()
        monkeypatch.setattr(main, "_api_failure_count", 0)
        monkeypatch.setattr(main, "_observe_only_mode", False)
        monkeypatch.setattr(main, "DRY_RUN", True)

        def _fail_if_called(*a, **k):
            raise AssertionError("must not poll the broker in DRY_RUN")

        monkeypatch.setattr(main, "_poll_order_fill", _fail_if_called)

        result = main.execute_trade("TST6")
        assert result["entry_price"] == pytest.approx(10.00)
        row = journal.query_recent_trades(limit=1)[0]
        assert row["data_quality_status"] == "verified"
