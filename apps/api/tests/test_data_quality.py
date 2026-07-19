"""
Tests for journal.py data-quality tracking and the shared performance-eligibility
predicate (journal.ELIGIBLE_TRADE_SQL). These prove a zero-price / unresolved /
unconfirmed-entry row can never corrupt P&L, win rate, expectancy, profit factor,
average loss, or drawdown — without deleting or rewriting the underlying row.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import journal


def _iso(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "dq_test.db")
    journal.init_db()


# ── Schema / defaults ────────────────────────────────────────────────────────────

def test_new_rows_default_to_verified(db):
    journal.open_paper_trade("SOFI", {
        "entry_timestamp": _iso(30), "entry_price": 10.0, "stop_price": 9.5,
        "take_profit_price": 11.0, "qty": 10,
    })
    journal.close_paper_trade("SOFI", 10.5, "take_profit_hit")
    row = journal.query_recent_trades(limit=1)[0]
    assert row["data_quality_status"] == "verified"


def test_migrate_db_is_idempotent(db):
    # Calling init_db (and therefore _migrate_db) twice must not error or duplicate columns.
    journal.init_db()
    journal.init_db()
    with journal._conn() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(paper_trades)")}
    assert "data_quality_status" in cols
    assert "data_quality_note" in cols


# ── close_paper_trade data-quality behavior ───────────────────────────────────────

def test_close_with_none_price_leaves_pnl_null_and_no_price_fabricated(db):
    journal.open_paper_trade("RIOT", {
        "entry_timestamp": _iso(60), "entry_price": 22.99, "stop_price": 22.53,
        "take_profit_price": 23.91, "qty": 8,
    })
    journal.close_paper_trade(
        "RIOT", None, "unresolved_reconciliation",
        data_quality_status="unresolved_reconciliation",
        data_quality_note="no broker fill found",
    )
    row = journal.query_recent_trades(limit=1)[0]
    assert row["exit_price"] is None
    assert row["realized_pnl"] is None
    assert row["realized_pnl_pct"] is None
    assert row["realized_r_multiple"] is None
    assert row["data_quality_status"] == "unresolved_reconciliation"


def test_unknown_data_quality_status_rejected(db):
    journal.open_paper_trade("BAC", {
        "entry_timestamp": _iso(30), "entry_price": 52.34, "stop_price": 51.29,
        "take_profit_price": 54.43, "qty": 3,
    })
    with pytest.raises(ValueError):
        journal.close_paper_trade("BAC", 50.0, "signal_exit", data_quality_status="not_a_real_status")


def test_clean_exit_does_not_launder_pending_entry_fill(db):
    """A trade whose entry price was never confirmed stays suspect even if it closes cleanly."""
    journal.open_paper_trade("XLK", {
        "entry_timestamp": _iso(45), "entry_price": 200.0, "stop_price": 195.0,
        "take_profit_price": 210.0, "qty": 1,
        "data_quality_status": "pending_entry_fill",
        "data_quality_note": "order accepted but not confirmed filled within poll budget",
    })
    # Default close call — caller doesn't know (or shouldn't need to know) the entry
    # was flagged; a normal "verified" close must not erase the entry-side flag.
    journal.close_paper_trade("XLK", 205.0, "take_profit_hit")

    row = journal.query_recent_trades(limit=1)[0]
    assert row["data_quality_status"] == "pending_entry_fill"
    assert row["exit_price"] == 205.0  # real exit fill still recorded
    assert row["realized_pnl"] is not None  # PnL still computed for the raw record

    # But it must be excluded from performance stats since the entry price was never confirmed.
    perf = journal.query_performance_summary()
    assert perf.get("eligible_trade_count", 0) == 0
    assert perf.get("excluded_trade_count", 0) == 1


def test_explicit_status_on_close_overrides_pending_entry(db):
    """An explicit non-default status passed by the caller always wins."""
    journal.open_paper_trade("XLK", {
        "entry_timestamp": _iso(45), "entry_price": 200.0, "stop_price": 195.0,
        "take_profit_price": 210.0, "qty": 1,
        "data_quality_status": "pending_entry_fill",
    })
    journal.close_paper_trade("XLK", 0.0, "reconcile_stale", data_quality_status="suspect_zero_exit")
    row = journal.query_recent_trades(limit=1)[0]
    assert row["data_quality_status"] == "suspect_zero_exit"


# ── mark_paper_trade_data_quality (administrative, idempotent) ───────────────────

def test_mark_paper_trade_data_quality_is_idempotent_and_preserves_pnl(db):
    journal.open_paper_trade("RIOT", {
        "entry_timestamp": "2026-05-18T14:27:30.265578+00:00", "entry_price": 22.99,
        "stop_price": 22.53, "take_profit_price": 23.91, "qty": 8,
    })
    journal.close_paper_trade("RIOT", 0.0, "reconcile_stale")  # simulate the old unsafe write
    row = journal.query_recent_trades(limit=1)[0]
    trade_id = None
    with journal._conn() as con:
        trade_id = con.execute("SELECT id FROM paper_trades WHERE symbol='RIOT'").fetchone()[0]

    original_pnl = row["realized_pnl"]
    ok1 = journal.mark_paper_trade_data_quality(trade_id, "suspect_zero_exit", "known bad reconcile row")
    ok2 = journal.mark_paper_trade_data_quality(trade_id, "suspect_zero_exit", "known bad reconcile row")
    assert ok1 and ok2

    row = journal.query_recent_trades(limit=1)[0]
    assert row["data_quality_status"] == "suspect_zero_exit"
    assert row["exit_price"] == 0.0          # untouched
    assert row["realized_pnl"] == original_pnl  # untouched — no rewriting of history
    assert row["exit_reason"] == "reconcile_stale"  # untouched


def test_mark_paper_trade_data_quality_unknown_status_rejected(db):
    with pytest.raises(ValueError):
        journal.mark_paper_trade_data_quality(1, "totally_made_up")


# ── Performance-eligibility filtering (regression: cannot corrupt stats) ─────────

@pytest.fixture
def mixed_db(db):
    """One good trade, one legacy zero-exit reconciliation row, one unresolved row."""
    # query_symbol_performance() enumerates symbols from trade_events, so each
    # symbol needs a matching event row to show up in its results at all.
    for sym in ("SOFI", "RIOT", "BAC"):
        journal.log_event({"timestamp": _iso(), "symbol": sym, "signal": "BUY"})

    # Good trade: +50 pnl
    journal.open_paper_trade("SOFI", {
        "entry_timestamp": _iso(120), "entry_price": 10.0, "stop_price": 9.5,
        "take_profit_price": 11.0, "qty": 10,
    })
    journal.close_paper_trade("SOFI", 15.0, "take_profit_hit")

    # Legacy-style bad row: exit_price=0.0, huge fabricated loss
    journal.open_paper_trade("RIOT", {
        "entry_timestamp": _iso(200), "entry_price": 22.99, "stop_price": 22.53,
        "take_profit_price": 23.91, "qty": 8,
    })
    journal.close_paper_trade(
        "RIOT", 0.0, "reconcile_stale", data_quality_status="suspect_zero_exit",
    )

    # Unresolved reconciliation: no price at all
    journal.open_paper_trade("BAC", {
        "entry_timestamp": _iso(150), "entry_price": 52.34, "stop_price": 51.29,
        "take_profit_price": 54.43, "qty": 3,
    })
    journal.close_paper_trade(
        "BAC", None, "unresolved_reconciliation",
        data_quality_status="unresolved_reconciliation",
    )
    return None


def test_query_performance_summary_excludes_suspect_rows(mixed_db):
    perf = journal.query_performance_summary()
    assert perf["eligible_trade_count"] == 1
    assert perf["excluded_trade_count"] == 2
    assert perf["total_simulated_pnl"] == pytest.approx(50.0)
    assert perf["win_rate"] == 100.0
    assert perf["largest_loser"] == pytest.approx(50.0)  # the fabricated -100% loss never appears
    assert perf["data_quality_warning"] is not None


def test_query_symbol_performance_excludes_suspect_rows(mixed_db):
    results = {r["symbol"]: r for r in journal.query_symbol_performance()}
    assert results["RIOT"]["eligible_trade_count"] == 0
    assert results["RIOT"]["excluded_trade_count"] == 1
    assert results["SOFI"]["eligible_trade_count"] == 1
    assert results["SOFI"]["total_pnl"] == pytest.approx(50.0)


def test_query_stable_v2_performance_excludes_suspect_rows(mixed_db):
    far_past = "2020-01-01T00:00:00+00:00"
    stable = journal.query_stable_v2_performance(far_past)
    assert stable["eligible_trade_count"] == 1
    assert stable["excluded_trade_count"] == 2
    assert stable["total_pnl"] == pytest.approx(50.0)


def test_query_recent_trades_is_unfiltered_but_labeled(mixed_db):
    """Raw history endpoints must not hide excluded rows — only performance stats exclude them."""
    rows = journal.query_recent_trades(limit=10)
    assert len(rows) == 3
    statuses = {r["symbol"]: r["data_quality_status"] for r in rows}
    assert statuses["SOFI"] == "verified"
    assert statuses["RIOT"] == "suspect_zero_exit"
    assert statuses["BAC"] == "unresolved_reconciliation"
