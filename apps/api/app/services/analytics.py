"""
Historical analytics service — read-only queries over the trading journal.

Every function in this module only SELECTs from the existing SQLite journal
(trade_events, paper_trades). Nothing here writes to the database and nothing
touches strategy, scoring, risk, or order execution.

Dates are the UTC calendar date of the stored timestamp. For this intraday
bot that is identical to the ET trading day, because the trading window
closes hours before midnight UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import journal

_ET = ZoneInfo("America/New_York")


def _cutoff_date(days: int) -> str:
    """YYYY-MM-DD string `days` calendar days back from now (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _day_of(ts: Optional[str]) -> Optional[str]:
    """YYYY-MM-DD portion of an ISO timestamp string."""
    return ts[:10] if ts else None


def _et_hour(ts: str) -> Optional[int]:
    """Hour of day in ET for an ISO UTC timestamp, or None if unparseable."""
    try:
        return datetime.fromisoformat(ts).astimezone(_ET).hour
    except Exception:
        return None


def _fetch_closed_trades(days: int, symbol: Optional[str] = None) -> list:
    """Closed trades exited on/after the cutoff, oldest first."""
    cutoff = _cutoff_date(days)
    query = (
        "SELECT symbol, entry_timestamp, exit_timestamp, entry_price, exit_price, "
        "exit_reason, realized_pnl, realized_pnl_pct, realized_r_multiple, "
        "hold_time_minutes, qty, entry_tier, entry_score, entry_grade "
        "FROM paper_trades "
        "WHERE is_open=0 AND realized_pnl IS NOT NULL AND exit_timestamp >= ?"
    )
    params: list = [cutoff]
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    query += " ORDER BY exit_timestamp ASC"
    with journal._conn() as con:
        return [dict(r) for r in con.execute(query, params).fetchall()]


def _group_by_day(trades: list) -> dict:
    """Group closed trades by exit date → {day: [trades]} in chronological order."""
    days: dict = {}
    for t in trades:
        day = _day_of(t.get("exit_timestamp"))
        if day:
            days.setdefault(day, []).append(t)
    return days


def _day_stats(day: str, trades: list) -> dict:
    """Aggregate one trading day's closed trades into a stats row."""
    pnls  = [t["realized_pnl"] for t in trades]
    wins  = [p for p in pnls if p > 0]
    rs    = [t["realized_r_multiple"] for t in trades if t["realized_r_multiple"] is not None]
    holds = [t["hold_time_minutes"] for t in trades if t["hold_time_minutes"] is not None]
    best  = max(trades, key=lambda t: t["realized_pnl"])
    worst = min(trades, key=lambda t: t["realized_pnl"])
    return {
        "date":             day,
        "trades":           len(trades),
        "wins":             len(wins),
        "losses":           len(pnls) - len(wins),
        "win_rate":         round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "total_pnl":        round(sum(pnls), 4),
        "avg_r_multiple":   round(sum(rs) / len(rs), 3) if rs else None,
        "avg_hold_minutes": round(sum(holds) / len(holds), 1) if holds else None,
        "best_trade":       {"symbol": best["symbol"], "pnl": round(best["realized_pnl"], 4)},
        "worst_trade":      {"symbol": worst["symbol"], "pnl": round(worst["realized_pnl"], 4)},
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def get_equity_curve(days: int = 30) -> dict:
    """Daily realized PnL and cumulative equity curve over the last N days."""
    try:
        by_day = _group_by_day(_fetch_closed_trades(days))
        points = []
        cumulative = 0.0
        for day in sorted(by_day):
            daily_pnl = sum(t["realized_pnl"] for t in by_day[day])
            cumulative += daily_pnl
            points.append({
                "date":           day,
                "daily_pnl":      round(daily_pnl, 4),
                "cumulative_pnl": round(cumulative, 4),
                "trades":         len(by_day[day]),
            })
        return {
            "days_requested": days,
            "trading_days":   len(points),
            "total_pnl":      round(cumulative, 4),
            "points":         points,
        }
    except Exception as e:
        return {"error": str(e)}


def get_daily_history(days: int = 30) -> dict:
    """One stats row per trading day over the last N days, oldest first."""
    try:
        by_day = _group_by_day(_fetch_closed_trades(days))
        rows = [_day_stats(day, by_day[day]) for day in sorted(by_day)]
        return {
            "days_requested": days,
            "trading_days":   len(rows),
            "sessions":       rows,
        }
    except Exception as e:
        return {"error": str(e)}


def get_symbol_history(symbol: str, days: int = 30) -> dict:
    """Date-windowed performance for one symbol, with a per-day breakdown."""
    try:
        symbol = symbol.upper()
        trades = _fetch_closed_trades(days, symbol=symbol)
        cutoff = _cutoff_date(days)
        with journal._conn() as con:
            setups = con.execute(
                "SELECT COUNT(*) FROM trade_events WHERE symbol=? AND timestamp >= ?",
                (symbol, cutoff),
            ).fetchone()[0]
            blocked_rows = con.execute(
                "SELECT blocked_by, COUNT(*) as cnt FROM trade_events "
                "WHERE symbol=? AND timestamp >= ? AND blocked_by IS NOT NULL "
                "GROUP BY blocked_by ORDER BY cnt DESC",
                (symbol, cutoff),
            ).fetchall()

        pnls = [t["realized_pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        rs   = [t["realized_r_multiple"] for t in trades if t["realized_r_multiple"] is not None]
        by_day = _group_by_day(trades)
        return {
            "symbol":            symbol,
            "days_requested":    days,
            "setups_evaluated":  setups,
            "closed_trades":     len(trades),
            "win_rate":          round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "total_pnl":         round(sum(pnls), 4) if pnls else 0.0,
            "avg_r_multiple":    round(sum(rs) / len(rs), 3) if rs else None,
            "blocker_breakdown": {r["blocked_by"]: r["cnt"] for r in blocked_rows},
            "daily":             [_day_stats(day, by_day[day]) for day in sorted(by_day)],
        }
    except Exception as e:
        return {"error": str(e)}


def get_trend_metrics(window: int = 5) -> dict:
    """
    Rolling performance trend: the most recent `window` trading sessions
    compared against the `window` sessions before them.
    """
    try:
        # Look back far enough to find 2×window trading sessions.
        by_day = _group_by_day(_fetch_closed_trades(days=window * 10))
        sessions = [_day_stats(day, by_day[day]) for day in sorted(by_day)]
        recent = sessions[-window:]
        prior  = sessions[-2 * window:-window]

        def _bucket(rows: list) -> dict:
            trades = sum(r["trades"] for r in rows)
            wins   = sum(r["wins"] for r in rows)
            pnl    = sum(r["total_pnl"] for r in rows)
            rs     = [r["avg_r_multiple"] for r in rows if r["avg_r_multiple"] is not None]
            return {
                "sessions":   len(rows),
                "trades":     trades,
                "win_rate":   round(wins / trades * 100, 1) if trades else None,
                "total_pnl":  round(pnl, 4),
                "expectancy": round(pnl / trades, 4) if trades else None,
                "avg_r":      round(sum(rs) / len(rs), 3) if rs else None,
            }

        def _direction(now: Optional[float], before: Optional[float], tolerance: float) -> str:
            if now is None or before is None:
                return "insufficient_data"
            if now > before + tolerance:
                return "improving"
            if now < before - tolerance:
                return "declining"
            return "flat"

        recent_b, prior_b = _bucket(recent), _bucket(prior)
        return {
            "window_sessions": window,
            "recent":          recent_b,
            "prior":           prior_b,
            "trend": {
                "win_rate":   _direction(recent_b["win_rate"], prior_b["win_rate"], tolerance=2.0),
                "expectancy": _direction(recent_b["expectancy"], prior_b["expectancy"], tolerance=0.5),
                "avg_r":      _direction(recent_b["avg_r"], prior_b["avg_r"], tolerance=0.05),
            },
        }
    except Exception as e:
        return {"error": str(e)}


def get_blocker_history(days: int = 30) -> dict:
    """Entry-gate block frequency over time, from trade_events."""
    try:
        cutoff = _cutoff_date(days)
        with journal._conn() as con:
            rows = con.execute(
                "SELECT substr(timestamp, 1, 10) as day, blocked_by, COUNT(*) as cnt "
                "FROM trade_events "
                "WHERE timestamp >= ? AND blocked_by IS NOT NULL "
                "GROUP BY day, blocked_by ORDER BY day ASC",
                (cutoff,),
            ).fetchall()

        daily: dict = {}
        totals: dict = {}
        for r in rows:
            daily.setdefault(r["day"], {})[r["blocked_by"]] = r["cnt"]
            totals[r["blocked_by"]] = totals.get(r["blocked_by"], 0) + r["cnt"]

        total_blocks = sum(totals.values())
        ranked = [
            {
                "blocker": name,
                "count":   count,
                "share_pct": round(count / total_blocks * 100, 1) if total_blocks else 0.0,
            }
            for name, count in sorted(totals.items(), key=lambda x: -x[1])
        ]
        return {
            "days_requested": days,
            "total_blocks":   total_blocks,
            "blockers":       ranked,
            "daily":          [{"date": d, "blockers": daily[d]} for d in sorted(daily)],
        }
    except Exception as e:
        return {"error": str(e)}


def get_time_of_day_stats(days: int = 30) -> dict:
    """Win/loss clustering by ET entry hour, for closed trades in the window."""
    try:
        buckets: dict = {}
        for t in _fetch_closed_trades(days):
            hour = _et_hour(t["entry_timestamp"])
            if hour is None:
                continue
            b = buckets.setdefault(hour, {"trades": 0, "wins": 0, "total_pnl": 0.0})
            b["trades"] += 1
            if t["realized_pnl"] > 0:
                b["wins"] += 1
            b["total_pnl"] += t["realized_pnl"]

        hours = []
        for hour in sorted(buckets):
            b = buckets[hour]
            hours.append({
                "et_hour":   hour,
                "trades":    b["trades"],
                "wins":      b["wins"],
                "win_rate":  round(b["wins"] / b["trades"] * 100, 1),
                "total_pnl": round(b["total_pnl"], 4),
            })
        return {"days_requested": days, "hours": hours}
    except Exception as e:
        return {"error": str(e)}


def get_tier_performance(days: int = 30) -> dict:
    """Closed-trade performance grouped by entry tier over the window."""
    try:
        tiers: dict = {}
        for t in _fetch_closed_trades(days):
            tier = t.get("entry_tier") or "unknown"
            b = tiers.setdefault(tier, {"trades": 0, "wins": 0, "total_pnl": 0.0, "rs": []})
            b["trades"] += 1
            if t["realized_pnl"] > 0:
                b["wins"] += 1
            b["total_pnl"] += t["realized_pnl"]
            if t["realized_r_multiple"] is not None:
                b["rs"].append(t["realized_r_multiple"])

        result = []
        for tier, b in sorted(tiers.items(), key=lambda x: -x[1]["total_pnl"]):
            result.append({
                "entry_tier": tier,
                "trades":     b["trades"],
                "win_rate":   round(b["wins"] / b["trades"] * 100, 1),
                "total_pnl":  round(b["total_pnl"], 4),
                "avg_r":      round(sum(b["rs"]) / len(b["rs"]), 3) if b["rs"] else None,
            })
        return {"days_requested": days, "tiers": result}
    except Exception as e:
        return {"error": str(e)}
