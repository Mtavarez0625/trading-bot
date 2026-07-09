"""Tests for the historical analytics service (app/services/analytics.py)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import journal
from app.services import analytics


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _iso(days_ago: int, hour_utc: int = 15) -> str:
    """ISO UTC timestamp `days_ago` days back at the given UTC hour."""
    dt = datetime.now(timezone.utc).replace(
        hour=hour_utc, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_ago)
    return dt.isoformat()


def _insert_closed_trade(
    con,
    symbol: str,
    days_ago: int,
    pnl: float,
    r: float = 1.0,
    tier: str = "strong",
    exit_reason: str = "take_profit",
    hour_utc: int = 15,
):
    con.execute(
        "INSERT INTO paper_trades "
        "(symbol, entry_timestamp, exit_timestamp, entry_price, exit_price, "
        " exit_reason, realized_pnl, realized_pnl_pct, realized_r_multiple, "
        " hold_time_minutes, qty, entry_tier, is_open) "
        "VALUES (?, ?, ?, 10.0, ?, ?, ?, ?, ?, 30.0, 10, ?, 0)",
        (
            symbol,
            _iso(days_ago, hour_utc),
            _iso(days_ago, hour_utc + 1),
            10.0 + pnl / 10,
            exit_reason,
            pnl,
            pnl,
            r,
            tier,
        ),
    )


def _insert_blocked_event(con, symbol: str, days_ago: int, blocker: str,
                          score: int = 60, grade: str = "B"):
    con.execute(
        "INSERT INTO trade_events (timestamp, symbol, signal, blocked_by, score, grade) "
        "VALUES (?, ?, 'BUY', ?, ?, ?)",
        (_iso(days_ago), symbol, blocker, score, grade),
    )


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Point the journal at a temp DB seeded with two weeks of synthetic data."""
    db_path = tmp_path / "test_journal.db"
    monkeypatch.setattr(journal, "DB_PATH", db_path)
    journal.init_db()

    with journal._conn() as con:
        # Recent 5 sessions (days 1-5): mostly winners
        _insert_closed_trade(con, "SOFI", 1, pnl=25.0, r=1.5)
        _insert_closed_trade(con, "HOOD", 1, pnl=-10.0, r=-1.0, exit_reason="stop_loss")
        _insert_closed_trade(con, "PLTR", 2, pnl=30.0, r=2.0)
        _insert_closed_trade(con, "SOFI", 3, pnl=15.0, r=1.0)
        _insert_closed_trade(con, "RIOT", 4, pnl=20.0, r=1.2)
        _insert_closed_trade(con, "MARA", 5, pnl=12.0, r=0.8)
        # Prior 5 sessions (days 8-12): mostly losers, RIOT stopped repeatedly
        _insert_closed_trade(con, "RIOT", 8, pnl=-15.0, r=-1.0, exit_reason="stop_loss")
        _insert_closed_trade(con, "RIOT", 9, pnl=-12.0, r=-1.0, exit_reason="stop_loss")
        _insert_closed_trade(con, "RIOT", 10, pnl=-14.0, r=-1.0, exit_reason="stop_loss")
        _insert_closed_trade(con, "SNAP", 11, pnl=8.0, r=0.5)
        _insert_closed_trade(con, "BAC", 12, pnl=-5.0, r=-0.4, exit_reason="stop_loss")
        # Blocked evaluations
        for d in range(1, 6):
            _insert_blocked_event(con, "PLTR", d, "score_too_low", score=70, grade="B")
            _insert_blocked_event(con, "SNAP", d, "spread_too_wide", score=88, grade="A")
        for d in range(8, 13):
            _insert_blocked_event(con, "PLTR", d, "score_too_low", score=65, grade="B")

    return db_path


# ── Equity curve ──────────────────────────────────────────────────────────────

def test_equity_curve_cumulative_sums(seeded_db):
    result = analytics.get_equity_curve(days=30)
    assert "error" not in result
    points = result["points"]
    assert result["trading_days"] == len(points) > 0
    # Points chronological, cumulative is a running sum of daily
    running = 0.0
    for p in points:
        running += p["daily_pnl"]
        assert p["cumulative_pnl"] == pytest.approx(running, abs=0.01)
    assert result["total_pnl"] == pytest.approx(sum(p["daily_pnl"] for p in points), abs=0.01)


def test_equity_curve_respects_window(seeded_db):
    narrow = analytics.get_equity_curve(days=6)
    wide = analytics.get_equity_curve(days=30)
    assert narrow["trading_days"] < wide["trading_days"]
    # Recent-only window excludes the losing prior week
    assert narrow["total_pnl"] > wide["total_pnl"]


# ── Daily history ─────────────────────────────────────────────────────────────

def test_daily_history_day_stats(seeded_db):
    result = analytics.get_daily_history(days=30)
    sessions = result["sessions"]
    assert sessions == sorted(sessions, key=lambda s: s["date"])
    # Day with SOFI win + HOOD stop: 2 trades, 1 win, net +15
    two_trade_day = next(s for s in sessions if s["trades"] == 2)
    assert two_trade_day["wins"] == 1
    assert two_trade_day["losses"] == 1
    assert two_trade_day["win_rate"] == 50.0
    assert two_trade_day["total_pnl"] == pytest.approx(15.0)
    assert two_trade_day["best_trade"]["symbol"] == "SOFI"
    assert two_trade_day["worst_trade"]["symbol"] == "HOOD"


def test_daily_history_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "empty.db")
    journal.init_db()
    result = analytics.get_daily_history(days=30)
    assert result["trading_days"] == 0
    assert result["sessions"] == []


# ── Symbol history ────────────────────────────────────────────────────────────

def test_symbol_history_windowed(seeded_db):
    result = analytics.get_symbol_history("riot", days=30)
    assert result["symbol"] == "RIOT"  # case-normalized
    assert result["closed_trades"] == 4
    assert result["win_rate"] == 25.0  # 1 win of 4
    assert result["total_pnl"] == pytest.approx(20.0 - 15.0 - 12.0 - 14.0)
    # Narrow window drops the losing prior-week trades
    recent = analytics.get_symbol_history("RIOT", days=6)
    assert recent["closed_trades"] == 1
    assert recent["total_pnl"] == pytest.approx(20.0)


def test_symbol_history_includes_blockers(seeded_db):
    result = analytics.get_symbol_history("SNAP", days=30)
    assert result["blocker_breakdown"].get("spread_too_wide") == 5


# ── Trend metrics ─────────────────────────────────────────────────────────────

def test_trend_metrics_detects_improvement(seeded_db):
    result = analytics.get_trend_metrics(window=5)
    assert "error" not in result
    # Recent week is winners, prior week is losers
    assert result["recent"]["total_pnl"] > result["prior"]["total_pnl"]
    assert result["trend"]["win_rate"] == "improving"
    assert result["trend"]["expectancy"] == "improving"


def test_trend_metrics_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "empty.db")
    journal.init_db()
    result = analytics.get_trend_metrics(window=5)
    assert result["trend"]["win_rate"] == "insufficient_data"


# ── Blocker history ───────────────────────────────────────────────────────────

def test_blocker_history_ranking(seeded_db):
    result = analytics.get_blocker_history(days=30)
    assert result["total_blocks"] == 15
    ranked = result["blockers"]
    assert ranked[0]["blocker"] == "score_too_low"
    assert ranked[0]["count"] == 10
    shares = sum(b["share_pct"] for b in ranked)
    assert shares == pytest.approx(100.0, abs=0.5)
    assert result["daily"] == sorted(result["daily"], key=lambda d: d["date"])


# ── Time of day & tier ────────────────────────────────────────────────────────

def test_time_of_day_stats(seeded_db):
    result = analytics.get_time_of_day_stats(days=30)
    assert result["hours"]
    total = sum(h["trades"] for h in result["hours"])
    assert total == 11
    for h in result["hours"]:
        assert 0 <= h["win_rate"] <= 100


def test_tier_performance(seeded_db):
    result = analytics.get_tier_performance(days=30)
    tiers = {t["entry_tier"]: t for t in result["tiers"]}
    assert tiers["strong"]["trades"] == 11
    assert tiers["strong"]["win_rate"] == pytest.approx(6 / 11 * 100, abs=0.1)
