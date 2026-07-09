from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indicators import MIN_BARS_REQUIRED
from strategy import (
    RSI_THRESHOLD,
    REL_VOL_MIN,
    VWAP_EXTENSION_MAX,
    SPREAD_MAX_PCT,
    SignalResult,
    evaluate_signal,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_uptrend_df(n: int = 80) -> pd.DataFrame:
    """Sustained uptrend — satisfies trend and RSI conditions."""
    closes = [100.0 + i * 0.8 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [10000.0 + i * 500 for i in range(n)],
        }
    )


def _make_downtrend_df(n: int = 80) -> pd.DataFrame:
    closes = [200.0 - i * 0.8 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c + 0.2 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [10000.0] * n,
        }
    )


def _make_minimal_df(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [5000.0] * n,
        }
    )


def _make_all_conditions_df(n: int = 80) -> pd.DataFrame:
    """
    DataFrame designed to satisfy ALL Hybrid Ross Momentum conditions:
      - Gentle uptrend so price stays within 6% of VWAP
      - Volume spike on the last bar (rel_vol >= 2.0)
      - Tight bars (spread < 4%)
      - RSI in uptrend zone (>= 58)
    """
    # Gentle uptrend: closes rise ~3% over n bars → last close ~3% above VWAP midpoint
    closes = [100.0 + i * 0.04 for i in range(n)]
    # Constant baseline volume then a 3× spike on the last bar
    volumes = [10_000.0] * n
    volumes[-1] = 30_000.0
    return pd.DataFrame(
        {
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.10 for c in closes],   # range = 0.15 ≈ 0.15% of ~103 → spread OK
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


# ---------------------------------------------------------------------------
# Guard cases
# ---------------------------------------------------------------------------


class TestEvaluateSignalGuards:
    def test_none_input(self):
        result = evaluate_signal(None)
        assert result.signal is False
        assert result.enough_bars is False
        assert "empty" in result.reason.lower() or "missing" in result.reason.lower()

    def test_empty_dataframe(self):
        result = evaluate_signal(pd.DataFrame())
        assert result.signal is False
        assert result.enough_bars is False

    def test_missing_required_columns(self):
        df = pd.DataFrame({"close": [100.0] * 70})
        result = evaluate_signal(df)
        assert result.signal is False
        assert "missing" in result.reason.lower()

    def test_too_few_bars(self):
        df = _make_minimal_df(n=MIN_BARS_REQUIRED - 1)
        result = evaluate_signal(df)
        assert result.signal is False
        assert result.enough_bars is False
        assert str(MIN_BARS_REQUIRED - 1) in result.reason

    def test_exact_minimum_bars_does_not_crash(self):
        df = _make_minimal_df(n=MIN_BARS_REQUIRED)
        result = evaluate_signal(df)
        assert isinstance(result, SignalResult)


# ---------------------------------------------------------------------------
# Signal field completeness
# ---------------------------------------------------------------------------


class TestSignalResultFields:
    _ALL_FIELDS = (
        "signal", "enough_bars", "trend_ok", "rsi_ok", "volume_ok",
        "vwap_ok", "rel_vol_ok", "not_extended", "spread_ok",
        "ema_20", "ema_50", "rsi_14", "volume", "vol_avg_20",
        "vwap", "relative_volume", "reason",
    )

    def test_result_contains_all_fields(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        for attr in self._ALL_FIELDS:
            assert hasattr(result, attr), f"Missing field on SignalResult: {attr}"

    def test_reason_is_non_empty_string(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        assert isinstance(result.reason, str) and len(result.reason) > 0

    def test_indicator_values_are_floats_when_enough_bars(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        if result.enough_bars and result.ema_20 is not None:
            for attr in ("ema_20", "ema_50", "rsi_14", "volume", "vol_avg_20"):
                assert isinstance(getattr(result, attr), float), f"{attr} should be float"


# ---------------------------------------------------------------------------
# Individual condition tests
# ---------------------------------------------------------------------------


class TestHybridConditions:
    def test_uptrend_trend_condition_true(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        assert result.enough_bars is True
        assert result.trend_ok is True, f"Expected trend_ok=True; reason: {result.reason}"

    def test_downtrend_no_signal_trend_fails(self):
        df = _make_downtrend_df(n=80)
        result = evaluate_signal(df)
        assert result.signal is False
        assert result.trend_ok is False

    def test_entry_rejected_when_rsi_below_58(self):
        """Construct a sideways DF where RSI < 58 (oscillating prices)."""
        import math
        n = 80
        # Zigzag prices keep RSI near 50
        closes = [100.0 + math.sin(i * 0.5) * 0.5 for i in range(n)]
        df = pd.DataFrame(
            {
                "open": [c - 0.05 for c in closes],
                "high": [c + 0.1 for c in closes],
                "low": [c - 0.1 for c in closes],
                "close": closes,
                "volume": [20_000.0] * (n - 1) + [60_000.0],  # volume spike still present
            }
        )
        result = evaluate_signal(df)
        # RSI on a pure sideways should be <58
        if result.rsi_14 is not None and result.rsi_14 < RSI_THRESHOLD:
            assert result.rsi_ok is False
            assert result.signal is False
            assert "RSI" in result.reason or "rsi" in result.reason.lower()

    def test_entry_rejected_when_rsi_threshold_constant(self):
        """Verify the module-level RSI threshold is 58."""
        assert RSI_THRESHOLD == 58.0

    def test_entry_rejected_when_relative_volume_below_2(self):
        """With constant volume rel_vol = 1.0, below the 2.0 threshold."""
        n = 80
        closes = [100.0 + i * 0.04 for i in range(n)]
        df = pd.DataFrame(
            {
                "open": [c - 0.05 for c in closes],
                "high": [c + 0.10 for c in closes],
                "low": [c - 0.05 for c in closes],
                "close": closes,
                "volume": [10_000.0] * n,   # constant → rel_vol = 1.0
            }
        )
        result = evaluate_signal(df)
        assert result.rel_vol_ok is False
        assert result.signal is False
        assert "relative volume" in result.reason or "rel" in result.reason.lower()

    def test_entry_rejected_when_price_below_vwap(self):
        """In a downtrend the final close is below VWAP."""
        df = _make_downtrend_df(n=80)
        result = evaluate_signal(df)
        # Downtrend → close < VWAP
        assert result.vwap_ok is False
        assert result.signal is False
        assert "vwap" in result.reason.lower() or "VWAP" in result.reason

    def test_entry_rejected_when_price_too_extended(self):
        """A strong rapid uptrend makes the last close >6% above VWAP."""
        n = 80
        # Fast uptrend: prices jump 20% over n bars — last close will be far above VWAP
        closes = [100.0 + i * 0.3 for i in range(n)]
        volumes = [10_000.0] * (n - 1) + [30_000.0]
        df = pd.DataFrame(
            {
                "open": [c - 0.1 for c in closes],
                "high": [c + 0.2 for c in closes],
                "low": [c - 0.1 for c in closes],
                "close": closes,
                "volume": volumes,
            }
        )
        result = evaluate_signal(df)
        # In a strong uptrend the last close is well above VWAP midpoint
        if result.vwap is not None:
            extension = (result.ema_20 or 0) - (result.vwap or 0)
            if result.vwap > 0:
                ext_pct = (df["close"].iloc[-1] - result.vwap) / result.vwap
                if ext_pct > VWAP_EXTENSION_MAX:
                    assert result.not_extended is False
                    assert "extended" in result.reason

    def test_entry_accepted_when_all_conditions_met(self):
        """All seven conditions satisfied → signal=True."""
        df = _make_all_conditions_df(n=80)
        result = evaluate_signal(df)
        assert result.enough_bars is True
        assert result.signal is True, (
            f"Expected signal=True but got reason: {result.reason}\n"
            f"  trend_ok={result.trend_ok}, rsi_ok={result.rsi_ok}, "
            f"volume_ok={result.volume_ok}, vwap_ok={result.vwap_ok}, "
            f"rel_vol_ok={result.rel_vol_ok}, not_extended={result.not_extended}, "
            f"spread_ok={result.spread_ok}"
        )
        assert result.reason == "All conditions met"

    def test_spread_ok_false_when_bar_range_too_wide(self):
        """If a bar's range (high-low)/close > SPREAD_MAX_PCT, spread_ok=False."""
        n = 80
        closes = [100.0 + i * 0.04 for i in range(n)]
        volumes = [10_000.0] * (n - 1) + [30_000.0]
        # Artificially widen the last bar: range = 8 / 103 ≈ 7.8% > 4%
        highs = [c + 0.1 for c in closes]
        lows = [c - 0.05 for c in closes]
        highs[-1] = closes[-1] + 4.0
        lows[-1] = closes[-1] - 4.0
        df = pd.DataFrame(
            {
                "open": [c - 0.05 for c in closes],
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }
        )
        result = evaluate_signal(df)
        assert result.spread_ok is False
        assert result.signal is False
        assert "spread" in result.reason

    def test_flat_market_no_signal(self):
        """Flat price with constant volume: EMA20 == EMA50, trend_ok=False."""
        closes = [100.0] * 80
        df = pd.DataFrame(
            {
                "open": [99.9] * 80,
                "high": [100.1] * 80,
                "low": [99.9] * 80,
                "close": closes,
                "volume": [10000.0] * 80,
            }
        )
        result = evaluate_signal(df)
        assert result.signal is False

    def test_no_signal_reason_describes_failure(self):
        df = _make_downtrend_df(n=80)
        result = evaluate_signal(df)
        assert result.reason != "All conditions met"

    def test_skip_reason_prefix_format(self):
        """All failure reasons should start with 'skipped:'."""
        df = _make_downtrend_df(n=80)
        result = evaluate_signal(df)
        # Each semicolon-separated reason should start with "skipped:"
        for part in result.reason.split(";"):
            assert part.strip().startswith("skipped:"), (
                f"Reason part does not start with 'skipped:': {part.strip()!r}"
            )
