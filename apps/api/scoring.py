"""
Candidate scoring: 0-100 composite score for trade setup quality.

Dimensions and weights:
  trend         (25 pts) — crossover strength (strong vs early)
  volume        (15 pts) — current vol vs 20-day average
  rsi           (15 pts) — momentum health (55-68 ideal)
  macd          (15 pts) — line above signal + histogram rising
  intraday      (10 pts) — close above intraday SMA20
  regime        (10 pts) — SPY/market is bullish
  affordability  (5 pts) — can buy ≥1 share at max allocation
  breakout       (5 pts) — price above N-bar high

Grades: A+ (≥85), A (≥75), B (≥65), C (<65)
Only BUY candidates receive a meaningful score; HOLD/SELL return score=0 / grade=C.
"""
from __future__ import annotations

import math
from typing import Optional


def compute_candidate_score(
    signal_data: dict,
    equity: float,
    max_allocation_pct: float = 0.20,
) -> dict:
    """
    Score a BUY candidate 0–100 across 8 dimensions.

    Args:
        signal_data: dict from _compute_raw_signal() / get_signal(), augmented with
                     spy_bullish, intraday_confirmed, intraday_margin_pct fields.
        equity:      effective account equity for affordability check.
        max_allocation_pct: max fraction of equity per single position.

    Returns:
        {score: int, grade: str, components: dict[str, int]}
    """
    pts: dict[str, int] = {}

    # 1. Trend (25 pts) ────────────────────────────────────────────────────────
    # strong-trend = confirmed SMA20 > SMA50 crossover
    # early-trend  = SMA20 approaching SMA50 and rising (pre-crossover)
    entry_tier = signal_data.get("entry_tier")           # "strong" | "early" | None
    trend_strength = float(signal_data.get("trend_strength") or 0.0)

    if entry_tier == "strong":
        # Scale 15–25 based on how decisive the crossover is (saturates at 5% gap)
        ts_scaled = min(trend_strength / 0.05, 1.0)
        pts["trend"] = 15 + int(round(ts_scaled * 10))
    elif entry_tier == "early":
        # Early trend earns 12–14; histogram rising adds the upper bound
        hist_rising = bool(signal_data.get("macd_histogram_rising"))
        pts["trend"] = 14 if hist_rising else 12
    else:
        pts["trend"] = 0

    # 2. Volume (15 pts) ───────────────────────────────────────────────────────
    current_vol = signal_data.get("current_volume") or 0
    vol_avg = signal_data.get("vol_sma_20") or 0
    vol_ratio: Optional[float] = (
        (current_vol / vol_avg) if (vol_avg > 0 and current_vol > 0) else None
    )

    if vol_ratio is not None:
        # 0.25× → 0 pts | 1.0× → 10 pts | ≥2.0× → 15 pts
        if vol_ratio >= 2.0:
            pts["volume"] = 15
        elif vol_ratio >= 1.0:
            pts["volume"] = 10 + int((vol_ratio - 1.0) * 5)
        elif vol_ratio >= 0.25:
            pts["volume"] = int(((vol_ratio - 0.25) / 0.75) * 10)
        else:
            pts["volume"] = 0
    elif signal_data.get("volume_confirmed"):
        pts["volume"] = 7  # data unavailable but not blocked — neutral
    else:
        pts["volume"] = 0

    # 3. RSI health (15 pts) ───────────────────────────────────────────────────
    # Sweet spot: 55–68 = momentum building without being overbought
    rsi = signal_data.get("rsi")
    if rsi is not None:
        rsi = float(rsi)
        if 55.0 <= rsi <= 68.0:
            pts["rsi"] = 15
        elif 50.0 <= rsi < 55.0 or 68.0 < rsi <= 72.0:
            pts["rsi"] = 10
        elif 45.0 <= rsi < 50.0 or 72.0 < rsi <= 76.0:
            pts["rsi"] = 5
        else:
            pts["rsi"] = 0
    else:
        pts["rsi"] = 7  # unavailable — neutral

    # 4. MACD (15 pts) ─────────────────────────────────────────────────────────
    macd_pts = 0
    if signal_data.get("macd_bullish"):          # line > signal line
        macd_pts += 9
    if signal_data.get("macd_histogram_rising"):  # momentum accelerating
        macd_pts += 6
    pts["macd"] = macd_pts

    # 5. Intraday confirmation (10 pts) ────────────────────────────────────────
    if signal_data.get("intraday_confirmed"):
        margin = float(signal_data.get("intraday_margin_pct") or 0.0)
        if margin >= 0.005:    # > 0.5% above intraday SMA20 — strong confirmation
            pts["intraday"] = 10
        elif margin >= 0.0:    # confirmed but slim margin
            pts["intraday"] = 7
        else:                  # tolerance-override (marginal pass)
            pts["intraday"] = 4
    else:
        pts["intraday"] = 0

    # 6. Market regime (10 pts) ────────────────────────────────────────────────
    pts["regime"] = 10 if signal_data.get("spy_bullish") else 0

    # 7. Affordability (5 pts) ─────────────────────────────────────────────────
    close = float(signal_data.get("close") or 0.0)
    max_usd = equity * max_allocation_pct
    if close > 0 and max_usd >= close:
        shares_possible = int(math.floor(max_usd / close))
        # 1 share = 1 pt, 2 = 2, ..., capped at 5
        pts["affordability"] = min(5, max(1, shares_possible))
    else:
        pts["affordability"] = 0  # symbol is unaffordable

    # 8. Breakout confirmation (5 pts) ─────────────────────────────────────────
    pts["breakout"] = 5 if signal_data.get("breakout_confirmed") else 0

    # ── Total & grade ──────────────────────────────────────────────────────────
    raw_score = sum(pts.values())
    score = max(0, min(100, raw_score))

    if score >= 85:
        grade = "A+"
    elif score >= 75:
        grade = "A"
    elif score >= 65:
        grade = "B"
    else:
        grade = "C"

    return {"score": score, "grade": grade, "components": pts}


def score_summary_line(symbol: str, score: int, grade: str, signal_data: dict) -> str:
    """One-line log summary: SOFI | score=78 [A] | trend=strong | vol=1.4x | RSI=62"""
    rsi = signal_data.get("rsi")
    rsi_tag = f" | RSI={rsi:.0f}" if rsi is not None else ""

    current_vol = signal_data.get("current_volume") or 0
    vol_avg = signal_data.get("vol_sma_20") or 0
    if vol_avg > 0 and current_vol > 0:
        vol_ratio = current_vol / vol_avg
        vol_tag = f" | vol={vol_ratio:.1f}x"
    else:
        vol_tag = ""

    tier = signal_data.get("entry_tier") or "?"
    macd_ok = signal_data.get("macd_bullish")
    macd_tag = " MACD↑" if macd_ok else " MACD↓"

    return (
        f"{symbol} | score={score} [{grade}] | tier={tier}"
        f"{vol_tag}{rsi_tag}{macd_tag}"
    )
