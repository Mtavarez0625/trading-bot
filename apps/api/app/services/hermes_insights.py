"""
Hermes insights service — read-only, rule-based analysis across sessions.

SAFETY (per HERMES_RULES.md): everything in this module is report-only.
It never places trades, never starts/stops the bot, and never changes
strategy, risk, watchlist, or mode settings. It only reads the journal
via the analytics service and produces deterministic narratives.
"""

from __future__ import annotations

from typing import Optional

import journal
from app.services import analytics


# ── Weekly review ──────────────────────────────────────────────────────────────

def build_weekly_review() -> dict:
    """
    Compare the most recent 5 trading sessions against the 5 before them,
    with best/worst trades, dominant blockers, and questions worth reviewing.
    """
    try:
        trend = analytics.get_trend_metrics(window=5)
        if trend.get("error"):
            return trend
        recent, prior = trend["recent"], trend["prior"]

        closed = analytics._fetch_closed_trades(days=14)
        best  = max(closed, key=lambda t: t["realized_pnl"]) if closed else None
        worst = min(closed, key=lambda t: t["realized_pnl"]) if closed else None

        blockers = analytics.get_blocker_history(days=7)
        top_blockers = blockers.get("blockers", [])[:3]

        # Narrative
        narrative = []
        if recent["sessions"] == 0:
            narrative.append("No closed trades in the recent window — nothing to review yet.")
        else:
            narrative.append(
                f"Last {recent['sessions']} session(s): {recent['trades']} trade(s), "
                f"win rate {recent['win_rate']}%, total PnL ${recent['total_pnl']}."
            )
            if prior["sessions"] > 0:
                delta = round(recent["total_pnl"] - prior["total_pnl"], 4)
                word = "up" if delta > 0 else "down" if delta < 0 else "unchanged"
                narrative.append(
                    f"PnL is {word} ${abs(delta)} vs the prior {prior['sessions']} session(s) "
                    f"(win rate {prior['win_rate']}% → {recent['win_rate']}%)."
                )
            for metric, direction in trend["trend"].items():
                if direction in ("improving", "declining"):
                    narrative.append(f"{metric.replace('_', ' ').capitalize()} is {direction}.")
        if top_blockers:
            blist = ", ".join(f"{b['blocker']} ({b['count']}×)" for b in top_blockers)
            narrative.append(f"Most frequent entry blockers this week: {blist}.")

        # Questions Marcos should review (per HERMES_RULES.md)
        questions = []
        if recent["win_rate"] is not None and recent["win_rate"] < 40 and recent["trades"] >= 5:
            questions.append(
                f"Win rate over the last {recent['sessions']} sessions is {recent['win_rate']}% — "
                "are recent entries lower quality, or is the market regime unfavorable?"
            )
        if trend["trend"].get("expectancy") == "declining":
            questions.append("Expectancy is declining vs the prior window — worth reviewing recent losers.")
        if worst and worst["realized_pnl"] < 0 and abs(worst["realized_pnl"]) > 2 * abs(best["realized_pnl"] or 1):
            questions.append(
                f"Largest loss ({worst['symbol']}, ${round(worst['realized_pnl'], 2)}) dwarfs the largest "
                "win — did the stop work as intended on that trade?"
            )
        if top_blockers and top_blockers[0]["share_pct"] > 60:
            questions.append(
                f"'{top_blockers[0]['blocker']}' accounts for {top_blockers[0]['share_pct']}% of all blocks "
                "this week — is that gate behaving as expected?"
            )
        if not questions:
            questions.append("Nothing stands out — performance is within normal ranges.")

        def _trade_line(t: Optional[dict]) -> Optional[dict]:
            if not t:
                return None
            return {
                "symbol":     t["symbol"],
                "pnl":        round(t["realized_pnl"], 4),
                "r_multiple": t["realized_r_multiple"],
                "exit_reason": t["exit_reason"],
                "date":       (t["exit_timestamp"] or "")[:10],
            }

        return {
            "read_only":     True,
            "recent_window": recent,
            "prior_window":  prior,
            "trend":         trend["trend"],
            "best_trade":    _trade_line(best),
            "worst_trade":   _trade_line(worst),
            "top_blockers":  top_blockers,
            "narrative":     narrative,
            "questions_for_review": questions,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Cross-session pattern detection ────────────────────────────────────────────

def detect_patterns(days: int = 30) -> dict:
    """
    Rule-based pattern detection across sessions. Every pattern carries the
    numbers that support it, so it can be verified against the journal.
    """
    try:
        patterns: list = []
        closed = analytics._fetch_closed_trades(days)

        # 1. Symbols repeatedly hitting stops
        stop_counts: dict = {}
        for t in closed:
            reason = (t.get("exit_reason") or "").lower()
            if "stop" in reason:
                stop_counts[t["symbol"]] = stop_counts.get(t["symbol"], 0) + 1
        for sym, count in sorted(stop_counts.items(), key=lambda x: -x[1]):
            if count >= 3:
                sym_pnl = round(sum(t["realized_pnl"] for t in closed if t["symbol"] == sym), 4)
                patterns.append({
                    "pattern":  "repeated_stop_losses",
                    "severity": "warning",
                    "detail":   f"{sym} hit a stop {count}× in the last {days} days (net PnL ${sym_pnl}).",
                    "evidence": {"symbol": sym, "stop_count": count, "net_pnl": sym_pnl},
                })

        # 2. Entry-tier over/under-performance
        tier_stats = analytics.get_tier_performance(days)
        for tier in tier_stats.get("tiers", []):
            if tier["trades"] >= 5 and tier["win_rate"] < 35:
                patterns.append({
                    "pattern":  "underperforming_tier",
                    "severity": "warning",
                    "detail":   (
                        f"Tier '{tier['entry_tier']}' entries win only {tier['win_rate']}% "
                        f"over {tier['trades']} trades (PnL ${tier['total_pnl']})."
                    ),
                    "evidence": tier,
                })
            elif tier["trades"] >= 5 and tier["win_rate"] > 65:
                patterns.append({
                    "pattern":  "outperforming_tier",
                    "severity": "info",
                    "detail":   (
                        f"Tier '{tier['entry_tier']}' entries win {tier['win_rate']}% "
                        f"over {tier['trades']} trades (PnL ${tier['total_pnl']})."
                    ),
                    "evidence": tier,
                })

        # 3. Time-of-day clustering of winners/losers
        tod = analytics.get_time_of_day_stats(days)
        for h in tod.get("hours", []):
            if h["trades"] >= 5 and h["win_rate"] < 30:
                patterns.append({
                    "pattern":  "weak_entry_hour",
                    "severity": "info",
                    "detail":   (
                        f"Entries at {h['et_hour']}:00 ET win only {h['win_rate']}% "
                        f"over {h['trades']} trades (PnL ${h['total_pnl']})."
                    ),
                    "evidence": h,
                })

        # 4. Blocker spike: last 5 sessions vs the window average
        bh = analytics.get_blocker_history(days)
        daily = bh.get("daily", [])
        if len(daily) >= 8:
            recent_days, earlier_days = daily[-5:], daily[:-5]
            per_blocker_recent: dict = {}
            per_blocker_earlier: dict = {}
            for d in recent_days:
                for name, cnt in d["blockers"].items():
                    per_blocker_recent[name] = per_blocker_recent.get(name, 0) + cnt
            for d in earlier_days:
                for name, cnt in d["blockers"].items():
                    per_blocker_earlier[name] = per_blocker_earlier.get(name, 0) + cnt
            for name, recent_total in per_blocker_recent.items():
                recent_avg  = recent_total / len(recent_days)
                earlier_avg = per_blocker_earlier.get(name, 0) / len(earlier_days)
                if earlier_avg > 0 and recent_avg > 2 * earlier_avg and recent_total >= 10:
                    patterns.append({
                        "pattern":  "blocker_spike",
                        "severity": "warning",
                        "detail":   (
                            f"'{name}' blocks jumped to {round(recent_avg, 1)}/day in the last "
                            f"{len(recent_days)} sessions vs {round(earlier_avg, 1)}/day before."
                        ),
                        "evidence": {
                            "blocker":      name,
                            "recent_avg":   round(recent_avg, 1),
                            "earlier_avg":  round(earlier_avg, 1),
                            "recent_total": recent_total,
                        },
                    })

        if not patterns:
            patterns.append({
                "pattern":  "none_detected",
                "severity": "info",
                "detail":   f"No notable cross-session patterns in the last {days} days.",
                "evidence": {"closed_trades": len(closed)},
            })

        return {
            "read_only":      True,
            "days_analyzed":  days,
            "closed_trades":  len(closed),
            "patterns":       patterns,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Blocked-signal analysis ─────────────────────────────────────────────────────

def build_blocked_signal_analysis(days: int = 30, min_entry_score: Optional[int] = None) -> dict:
    """
    Which gates block entries most, and which blocked evaluations were
    near-misses (high score/grade blocked by a non-score gate, or a score
    within 10 points of the entry threshold).
    """
    try:
        bh = analytics.get_blocker_history(days)
        if bh.get("error"):
            return bh

        cutoff = analytics._cutoff_date(days)
        with journal._conn() as con:
            near_rows = con.execute(
                "SELECT symbol, substr(timestamp,1,10) as day, blocked_by, score, grade "
                "FROM trade_events "
                "WHERE timestamp >= ? AND blocked_by IS NOT NULL AND score IS NOT NULL "
                "ORDER BY score DESC LIMIT 200",
                (cutoff,),
            ).fetchall()

        near_misses = []
        for r in near_rows:
            score, grade, blocker = r["score"], r["grade"] or "", r["blocked_by"]
            high_grade_non_score = grade in ("A+", "A") and "score" not in (blocker or "").lower()
            near_threshold = (
                min_entry_score is not None
                and 0 <= (min_entry_score - score) <= 10
            )
            if high_grade_non_score or near_threshold:
                near_misses.append({
                    "symbol":  r["symbol"],
                    "date":    r["day"],
                    "score":   score,
                    "grade":   r["grade"],
                    "blocker": blocker,
                    "reason":  "high_grade_blocked_by_gate" if high_grade_non_score else "score_near_threshold",
                })
        near_misses = near_misses[:25]

        observations = []
        blockers = bh.get("blockers", [])
        if blockers:
            top = blockers[0]
            observations.append(
                f"'{top['blocker']}' is the most frequent gate: {top['count']} blocks "
                f"({top['share_pct']}% of {bh['total_blocks']} total) over {days} days."
            )
        gate_blocked_high_grade = [n for n in near_misses if n["reason"] == "high_grade_blocked_by_gate"]
        if gate_blocked_high_grade:
            syms = sorted({n["symbol"] for n in gate_blocked_high_grade})
            observations.append(
                f"{len(gate_blocked_high_grade)} A/A+ setup(s) were blocked by non-score gates "
                f"({', '.join(syms)}) — worth checking whether those gates fired correctly."
            )
        if not observations:
            observations.append(f"No entry blocks recorded in the last {days} days.")

        return {
            "read_only":       True,
            "days_analyzed":   days,
            "min_entry_score": min_entry_score,
            "total_blocks":    bh.get("total_blocks", 0),
            "gate_frequency":  blockers,
            "daily_breakdown": bh.get("daily", []),
            "near_misses":     near_misses,
            "observations":    observations,
        }
    except Exception as e:
        return {"error": str(e)}
