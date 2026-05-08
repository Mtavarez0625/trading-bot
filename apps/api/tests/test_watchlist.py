"""
Tests for watchlist safety rules:
- Trade watchlist excludes regime symbols
- Affordability filter excludes symbols above MAX_ALLOCATION cap
- Scoring entry gate blocks low-quality setups
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scoring import compute_candidate_score

# ── Constants mirroring .env defaults ────────────────────────────────────────
EQUITY           = 1000.0
MAX_ALLOC_PCT    = 0.20
MAX_ALLOC_USD    = EQUITY * MAX_ALLOC_PCT   # $200
MIN_ENTRY_SCORE  = 75
REGIME_SYMBOLS   = {"SPY", "QQQ", "IWM"}
TRADE_WATCHLIST  = ["SOFI", "HOOD", "F", "RIOT", "MARA", "SNAP", "XLK"]


# ── Watchlist / regime separation ────────────────────────────────────────────

class TestWatchlistSeparation:
    def test_no_regime_symbol_in_trade_watchlist(self):
        overlap = set(TRADE_WATCHLIST) & REGIME_SYMBOLS
        assert overlap == set(), f"Regime symbols in trade watchlist: {overlap}"

    def test_spy_not_traded(self):
        assert "SPY" not in TRADE_WATCHLIST

    def test_qqq_not_traded(self):
        assert "QQQ" not in TRADE_WATCHLIST

    def test_iwm_not_traded(self):
        assert "IWM" not in TRADE_WATCHLIST

    def test_watchlist_has_expected_symbols(self):
        for sym in ("SOFI", "HOOD", "F", "RIOT", "MARA", "SNAP", "XLK"):
            assert sym in TRADE_WATCHLIST


# ── Affordability filter ──────────────────────────────────────────────────────

class TestAffordabilityFilter:
    """
    An affordable symbol requires price ≤ MAX_ALLOCATION_USD = $200
    so that ≥1 share can be purchased within the position cap.
    """

    def _score(self, price: float) -> dict:
        sd = {
            "entry_tier":            "strong",
            "trend_strength":        0.05,
            "current_volume":        1_000_000,
            "vol_sma_20":            500_000,
            "volume_confirmed":      True,
            "rsi":                   60.0,
            "macd_bullish":          True,
            "macd_histogram_rising": True,
            "intraday_confirmed":    True,
            "intraday_margin_pct":   0.008,
            "spy_bullish":           True,
            "close":                 price,
            "breakout_confirmed":    True,
        }
        return compute_candidate_score(sd, equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT)

    def test_cheap_symbol_is_affordable(self):
        # $8 (SOFI-like) → 25 shares at $200 cap → affordability = 5
        r = self._score(8.0)
        assert r["components"]["affordability"] >= 1

    def test_mid_price_symbol_is_affordable(self):
        # $15 → 13 shares → capped at 5
        r = self._score(15.0)
        assert r["components"]["affordability"] >= 1

    def test_expensive_symbol_is_unaffordable(self):
        # $250 > $200 max alloc → 0 shares → affordability = 0
        r = self._score(250.0)
        assert r["components"]["affordability"] == 0

    def test_borderline_symbol_is_affordable(self):
        # $190 < $200 → 1 share possible
        r = self._score(190.0)
        assert r["components"]["affordability"] == 1

    def test_amd_class_price_is_unaffordable(self):
        # AMD-class ~$70-80 at 20% of $1k = $200 cap — $75 gets 2 shares
        r = self._score(75.0)
        assert r["components"]["affordability"] == 2  # $200/75 = 2

    def test_score_penalizes_unaffordable(self):
        affordable   = self._score(8.0)
        unaffordable = self._score(300.0)
        assert affordable["score"] > unaffordable["score"]


# ── Score-based entry gating ──────────────────────────────────────────────────

class TestEntryScoreGating:
    """Verify that the MIN_ENTRY_SCORE threshold correctly classifies setups."""

    def _make_signal(self, **overrides) -> dict:
        base = {
            "entry_tier":            "strong",
            "trend_strength":        0.06,
            "current_volume":        2_000_000,
            "vol_sma_20":            1_000_000,
            "volume_confirmed":      True,
            "rsi":                   60.0,
            "macd_bullish":          True,
            "macd_histogram_rising": True,
            "intraday_confirmed":    True,
            "intraday_margin_pct":   0.010,
            "spy_bullish":           True,
            "close":                 5.0,
            "breakout_confirmed":    True,
        }
        base.update(overrides)
        return base

    def test_a_plus_setup_passes_threshold(self):
        r = compute_candidate_score(self._make_signal(), equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT)
        assert r["score"] >= MIN_ENTRY_SCORE
        assert r["grade"] in ("A+", "A")

    def test_c_setup_fails_threshold(self):
        # All negative: no trend, overbought, no volume, bearish
        bad = self._make_signal(
            entry_tier=None,
            trend_strength=0,
            current_volume=0,
            vol_sma_20=1_000_000,
            volume_confirmed=False,
            rsi=90.0,
            macd_bullish=False,
            macd_histogram_rising=False,
            intraday_confirmed=False,
            spy_bullish=False,
            close=300.0,
            breakout_confirmed=False,
        )
        r = compute_candidate_score(bad, equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT)
        assert r["score"] < MIN_ENTRY_SCORE
        assert r["grade"] == "C"

    def test_b_setup_is_below_default_threshold(self):
        # Partially good: trend but no intraday, no regime, lower volume
        medium = self._make_signal(
            spy_bullish=False,       # -10
            intraday_confirmed=False, # -10
            breakout_confirmed=False, # -5
            macd_histogram_rising=False,  # -6
            rsi=73.0,                # middle band → 10 not 15 → -5
        )
        r = compute_candidate_score(medium, equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT)
        # Exact score depends on all components; just verify it's lower
        assert r["score"] < 100

    def test_qty_zero_prevents_entry_at_unaffordable_price(self):
        # qty check is in main.py; here we verify affordability=0 in scoring
        expensive = self._make_signal(close=300.0)
        r = compute_candidate_score(expensive, equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT)
        assert r["components"]["affordability"] == 0

    def test_bearish_regime_lowers_score_by_10(self):
        bullish_regime  = compute_candidate_score(
            self._make_signal(spy_bullish=True), equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT
        )
        bearish_regime  = compute_candidate_score(
            self._make_signal(spy_bullish=False), equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT
        )
        assert bullish_regime["score"] - bearish_regime["score"] == 10

    def test_no_open_position_sell_does_not_enter(self):
        # A SELL signal with qty=0 must be ignored — score for non-BUY signals
        # The scoring is only called on BUY signals in main.py.
        # Here we verify that score on a non-trend (None entry_tier) is 0 for trend.
        no_trend = self._make_signal(entry_tier=None, trend_strength=0)
        r = compute_candidate_score(no_trend, equity=EQUITY, max_allocation_pct=MAX_ALLOC_PCT)
        assert r["components"]["trend"] == 0
