"""Tests for the Hermes insights service (app/services/hermes_insights.py)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import journal
from app.services import hermes_insights


def _iso(days_ago: int, hour_utc: int = 15) -> str:
    dt = datetime.now(timezone.utc).replace(
        hour=hour_utc, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_ago)
    return dt.isoformat()


def _insert_closed_trade(con, symbol, days_ago, pnl, r=1.0, tier="strong",
                         exit_reason="take_profit"):
    con.execute(
        "INSERT INTO paper_trades "
        "(symbol, entry_timestamp, exit_timestamp, entry_price, exit_price, "
        " exit_reason, realized_pnl, realized_pnl_pct, realized_r_multiple, "
        " hold_time_minutes, qty, entry_tier, is_open) "
        "VALUES (?, ?, ?, 10.0, ?, ?, ?, ?, ?, 30.0, 10, ?, 0)",
        (symbol, _iso(days_ago), _iso(days_ago, 16), 10.0 + pnl / 10,
         exit_reason, pnl, pnl, r, tier),
    )


def _insert_blocked_event(con, symbol, days_ago, blocker, score=60, grade="B"):
    con.execute(
        "INSERT INTO trade_events (timestamp, symbol, signal, blocked_by, score, grade) "
        "VALUES (?, ?, 'BUY', ?, ?, ?)",
        (_iso(days_ago), symbol, blocker, score, grade),
    )


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "test_journal.db")
    journal.init_db()
    with journal._conn() as con:
        # Recent week: winners
        for d in range(1, 6):
            _insert_closed_trade(con, "SOFI", d, pnl=20.0, r=1.5)
        # Prior week: RIOT stopped out 3× (repeated-stop pattern)
        for d in range(8, 11):
            _insert_closed_trade(con, "RIOT", d, pnl=-15.0, r=-1.0,
                                 exit_reason="stop_loss")
        _insert_closed_trade(con, "SNAP", 11, pnl=5.0, r=0.4)
        _insert_closed_trade(con, "BAC", 12, pnl=-8.0, r=-0.6,
                             exit_reason="stop_loss")
        # Blocks: A-grade setup blocked by a non-score gate, plus score blocks
        _insert_blocked_event(con, "PLTR", 2, "spread_too_wide", score=90, grade="A")
        for d in range(1, 6):
            _insert_blocked_event(con, "HOOD", d, "score_too_low", score=70, grade="B")
    return tmp_path


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "empty.db")
    journal.init_db()
    return tmp_path


# ── Weekly review ─────────────────────────────────────────────────────────────

def test_weekly_review_structure_and_safety(seeded_db):
    review = hermes_insights.build_weekly_review()
    assert "error" not in review
    assert review["read_only"] is True
    for key in ("recent_window", "prior_window", "trend", "best_trade",
                "worst_trade", "top_blockers", "narrative", "questions_for_review"):
        assert key in review


def test_weekly_review_compares_windows(seeded_db):
    review = hermes_insights.build_weekly_review()
    assert review["recent_window"]["total_pnl"] > review["prior_window"]["total_pnl"]
    assert review["trend"]["win_rate"] == "improving"
    assert review["best_trade"]["symbol"] == "SOFI"
    assert review["worst_trade"]["symbol"] == "RIOT"
    assert any("Last 5 session" in line for line in review["narrative"])


def test_weekly_review_empty_db(empty_db):
    review = hermes_insights.build_weekly_review()
    assert "error" not in review
    assert review["best_trade"] is None
    assert review["questions_for_review"]  # always gives at least one line


# ── Pattern detection ─────────────────────────────────────────────────────────

def test_patterns_detects_repeated_stops(seeded_db):
    result = hermes_insights.detect_patterns(days=30)
    assert result["read_only"] is True
    stops = [p for p in result["patterns"] if p["pattern"] == "repeated_stop_losses"]
    assert len(stops) == 1
    assert stops[0]["evidence"]["symbol"] == "RIOT"
    assert stops[0]["evidence"]["stop_count"] == 3


def test_patterns_tier_performance(seeded_db):
    result = hermes_insights.detect_patterns(days=30)
    # 'strong' tier: 6 wins of 10 → 60% — neither under (<35) nor over (>65)
    tier_patterns = [p for p in result["patterns"]
                     if p["pattern"] in ("underperforming_tier", "outperforming_tier")]
    assert tier_patterns == []


def test_patterns_empty_db(empty_db):
    result = hermes_insights.detect_patterns(days=30)
    assert result["closed_trades"] == 0
    assert result["patterns"][0]["pattern"] == "none_detected"


# ── Blocked-signal analysis ───────────────────────────────────────────────────

def test_blocked_analysis_gate_frequency(seeded_db):
    result = hermes_insights.build_blocked_signal_analysis(days=30, min_entry_score=75)
    assert result["read_only"] is True
    assert result["total_blocks"] == 6
    top = result["gate_frequency"][0]
    assert top["blocker"] == "score_too_low"
    assert top["count"] == 5


def test_blocked_analysis_near_misses(seeded_db):
    result = hermes_insights.build_blocked_signal_analysis(days=30, min_entry_score=75)
    reasons = {(n["symbol"], n["reason"]) for n in result["near_misses"]}
    # A-grade PLTR blocked by spread gate
    assert ("PLTR", "high_grade_blocked_by_gate") in reasons
    # HOOD score=70 within 10 of threshold 75
    assert ("HOOD", "score_near_threshold") in reasons


def test_blocked_analysis_empty_db(empty_db):
    result = hermes_insights.build_blocked_signal_analysis(days=30, min_entry_score=75)
    assert result["total_blocks"] == 0
    assert result["near_misses"] == []
    assert result["observations"]
