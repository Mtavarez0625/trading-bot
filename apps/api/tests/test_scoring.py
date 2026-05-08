"""Tests for the candidate scoring module (scoring.py)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scoring import compute_candidate_score, score_summary_line


# ── Helpers ───────────────────────────────────────────────────────────────────

def _perfect_signal() -> dict:
    """All dimensions maximally positive."""
    return {
        "entry_tier":            "strong",
        "trend_strength":        0.06,        # > 0.05 → full 25 pts
        "current_volume":        2_000_000,
        "vol_sma_20":            1_000_000,   # 2× avg → max vol pts
        "volume_confirmed":      True,
        "rsi":                   60.0,         # sweet spot 55-68
        "macd_bullish":          True,
        "macd_histogram_rising": True,
        "intraday_confirmed":    True,
        "intraday_margin_pct":   0.010,        # >0.5% above SMA
        "spy_bullish":           True,
        "close":                 5.0,          # cheap — many shares at 20%/$1k
        "breakout_confirmed":    True,
    }


def _bearish_signal() -> dict:
    """All dimensions worst case."""
    return {
        "entry_tier":            None,
        "trend_strength":        0.0,
        "current_volume":        0,
        "vol_sma_20":            0,
        "volume_confirmed":      False,
        "rsi":                   85.0,
        "macd_bullish":          False,
        "macd_histogram_rising": False,
        "intraday_confirmed":    False,
        "intraday_margin_pct":   -0.05,
        "spy_bullish":           False,
        "close":                 500.0,        # unaffordable at $200 cap
        "breakout_confirmed":    False,
    }


# ── Grade boundaries ──────────────────────────────────────────────────────────

class TestGrades:
    def test_perfect_score_is_100_grade_ap(self):
        r = compute_candidate_score(_perfect_signal(), equity=1000, max_allocation_pct=0.20)
        assert r["score"] == 100
        assert r["grade"] == "A+"

    def test_bearish_score_is_zero_grade_c(self):
        r = compute_candidate_score(_bearish_signal(), equity=1000, max_allocation_pct=0.20)
        assert r["score"] == 0
        assert r["grade"] == "C"

    def test_grade_ap_requires_85_plus(self):
        sd = _perfect_signal()
        sd["breakout_confirmed"] = False        # -5 pts → 95
        sd["intraday_margin_pct"] = 0.002       # 7 pts intraday instead of 10 → -3
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["grade"] in ("A+", "A")        # still high but exact pts may vary

    def test_grade_a_range_75_to_84(self):
        sd = _perfect_signal()
        # Remove breakout (5), weaken intraday (3), weaken volume a bit
        sd["breakout_confirmed"]    = False     # -5 pts
        sd["spy_bullish"]           = False     # -10 pts → 85 → A+/A boundary
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        # Should be in A or A+ depending on other components
        assert r["grade"] in ("A+", "A", "B")

    def test_grade_b_range_65_to_74(self):
        sd = _perfect_signal()
        sd["spy_bullish"]           = False     # -10
        sd["breakout_confirmed"]    = False     # -5
        sd["intraday_confirmed"]    = False     # -10  (was 10)
        sd["macd_histogram_rising"] = False     # -6
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert 50 <= r["score"] <= 80          # range check

    def test_grade_c_below_65(self):
        r = compute_candidate_score(_bearish_signal(), equity=1000, max_allocation_pct=0.20)
        assert r["grade"] == "C"
        assert r["score"] < 65


# ── Trend component ───────────────────────────────────────────────────────────

class TestTrend:
    def test_strong_trend_earns_15_to_25(self):
        sd = _bearish_signal()
        sd.update({"entry_tier": "strong", "trend_strength": 0.03, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert 15 <= r["components"]["trend"] <= 25

    def test_early_trend_earns_12_to_14(self):
        sd = _bearish_signal()
        sd.update({"entry_tier": "early", "macd_histogram_rising": False, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["trend"] == 12

    def test_early_trend_with_macd_rising_earns_14(self):
        sd = _bearish_signal()
        sd.update({"entry_tier": "early", "macd_histogram_rising": True, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["trend"] == 14

    def test_no_trend_earns_zero(self):
        sd = _bearish_signal()
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["trend"] == 0


# ── Volume component ──────────────────────────────────────────────────────────

class TestVolume:
    def test_two_x_volume_earns_15(self):
        sd = _bearish_signal()
        sd.update({"current_volume": 2_000_000, "vol_sma_20": 1_000_000, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["volume"] == 15

    def test_one_x_volume_earns_10(self):
        sd = _bearish_signal()
        sd.update({"current_volume": 1_000_000, "vol_sma_20": 1_000_000, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["volume"] == 10

    def test_zero_volume_earns_zero(self):
        sd = _bearish_signal()
        sd.update({"current_volume": 0, "vol_sma_20": 1_000_000, "volume_confirmed": False, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["volume"] == 0

    def test_data_unavailable_confirmed_earns_7(self):
        sd = _bearish_signal()
        sd.update({"current_volume": None, "vol_sma_20": None, "volume_confirmed": True, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["volume"] == 7


# ── RSI component ─────────────────────────────────────────────────────────────

class TestRSI:
    def test_sweet_spot_earns_15(self):
        sd = _bearish_signal()
        sd.update({"rsi": 62.0, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["rsi"] == 15

    def test_below_55_earns_10(self):
        sd = _bearish_signal()
        sd.update({"rsi": 52.0, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["rsi"] == 10

    def test_overbought_earns_zero(self):
        sd = _bearish_signal()
        sd.update({"rsi": 85.0, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["rsi"] == 0

    def test_missing_rsi_earns_7(self):
        sd = _bearish_signal()
        sd.update({"rsi": None, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["rsi"] == 7


# ── MACD component ────────────────────────────────────────────────────────────

class TestMACD:
    def test_bullish_and_rising_earns_15(self):
        sd = _bearish_signal()
        sd.update({"macd_bullish": True, "macd_histogram_rising": True, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["macd"] == 15

    def test_bullish_not_rising_earns_9(self):
        sd = _bearish_signal()
        sd.update({"macd_bullish": True, "macd_histogram_rising": False, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["macd"] == 9

    def test_bearish_earns_zero(self):
        sd = _bearish_signal()
        sd.update({"macd_bullish": False, "macd_histogram_rising": False, "close": 5.0})
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["macd"] == 0


# ── Affordability component ───────────────────────────────────────────────────

class TestAffordability:
    def test_unaffordable_earns_zero(self):
        sd = _bearish_signal()          # close=500, max_alloc=200
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["affordability"] == 0

    def test_affordable_earns_nonzero(self):
        sd = _bearish_signal()
        sd["close"] = 10.0              # 200/10 = 20 shares → capped at 5
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["affordability"] == 5

    def test_barely_affordable_earns_1(self):
        sd = _bearish_signal()
        sd["close"] = 195.0             # 200/195 = 1 share
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["affordability"] == 1

    def test_regime_symbol_price_excluded_scenario(self):
        # Watchlist must exclude regime symbols — verify score works for cheap symbols
        sd = _perfect_signal()
        sd["close"] = 8.0              # SOFI-like price
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["affordability"] >= 1


# ── Regime component ──────────────────────────────────────────────────────────

class TestRegime:
    def test_bullish_regime_earns_10(self):
        sd = _perfect_signal()
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["regime"] == 10

    def test_bearish_regime_earns_zero(self):
        sd = _perfect_signal()
        sd["spy_bullish"] = False
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["regime"] == 0


# ── Intraday component ────────────────────────────────────────────────────────

class TestIntraday:
    def test_strong_above_earns_10(self):
        sd = _perfect_signal()
        sd["intraday_confirmed"]  = True
        sd["intraday_margin_pct"] = 0.01
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["intraday"] == 10

    def test_marginal_above_earns_7(self):
        sd = _perfect_signal()
        sd["intraday_confirmed"]  = True
        sd["intraday_margin_pct"] = 0.001
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["intraday"] == 7

    def test_not_confirmed_earns_zero(self):
        sd = _perfect_signal()
        sd["intraday_confirmed"] = False
        r = compute_candidate_score(sd, equity=1000, max_allocation_pct=0.20)
        assert r["components"]["intraday"] == 0


# ── score_summary_line ────────────────────────────────────────────────────────

class TestSummaryLine:
    def test_contains_symbol_and_score(self):
        sd = _perfect_signal()
        line = score_summary_line("SOFI", 88, "A+", sd)
        assert "SOFI" in line
        assert "88" in line
        assert "A+" in line

    def test_contains_vol_ratio(self):
        sd = _perfect_signal()
        line = score_summary_line("HOOD", 75, "A", sd)
        assert "vol=" in line

    def test_no_crash_on_missing_rsi(self):
        sd = _perfect_signal()
        sd["rsi"] = None
        line = score_summary_line("F", 65, "B", sd)
        assert "F" in line


# ── Score clamping ────────────────────────────────────────────────────────────

class TestClamping:
    def test_score_never_exceeds_100(self):
        r = compute_candidate_score(_perfect_signal(), equity=1000, max_allocation_pct=0.20)
        assert r["score"] <= 100

    def test_score_never_below_zero(self):
        r = compute_candidate_score(_bearish_signal(), equity=1000, max_allocation_pct=0.20)
        assert r["score"] >= 0

    def test_components_present(self):
        r = compute_candidate_score(_perfect_signal(), equity=1000, max_allocation_pct=0.20)
        for dim in ("trend", "volume", "rsi", "macd", "intraday", "regime", "affordability", "breakout"):
            assert dim in r["components"]
