from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indicators import MIN_BARS_REQUIRED
from strategy import SignalResult, evaluate_signal


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_uptrend_df(n: int = 80) -> pd.DataFrame:
    """Strong, sustained uptrend. Should satisfy trend + RSI conditions."""
    closes = [100.0 + i * 0.8 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.2 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            # Rising volume ensures current bar > rolling avg
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
        # May or may not signal; should not raise
        assert isinstance(result, SignalResult)


# ---------------------------------------------------------------------------
# Signal logic
# ---------------------------------------------------------------------------


class TestEvaluateSignalLogic:
    def test_uptrend_trend_condition_true(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        assert result.enough_bars is True
        assert result.trend_ok is True, f"Expected trend_ok=True; reason: {result.reason}"

    def test_downtrend_no_signal(self):
        df = _make_downtrend_df(n=80)
        result = evaluate_signal(df)
        assert result.signal is False
        assert result.trend_ok is False

    def test_result_contains_all_fields(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        for attr in (
            "signal", "enough_bars", "trend_ok", "rsi_ok", "volume_ok",
            "ema_20", "ema_50", "rsi_14", "volume", "vol_avg_20", "reason",
        ):
            assert hasattr(result, attr), f"Missing field on SignalResult: {attr}"

    def test_reason_is_non_empty_string(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        assert isinstance(result.reason, str) and len(result.reason) > 0

    def test_no_signal_reason_describes_failure(self):
        df = _make_downtrend_df(n=80)
        result = evaluate_signal(df)
        assert result.reason != "All conditions met"

    def test_indicator_values_are_floats_when_enough_bars(self):
        df = _make_uptrend_df(n=80)
        result = evaluate_signal(df)
        if result.enough_bars and result.ema_20 is not None:
            assert isinstance(result.ema_20, float)
            assert isinstance(result.ema_50, float)
            assert isinstance(result.rsi_14, float)
            assert isinstance(result.volume, float)
            assert isinstance(result.vol_avg_20, float)

    def test_flat_low_volume_no_signal(self):
        """Flat price with constant volume: vol_avg == current vol, so volume_ok=False."""
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
        # Flat price: EMA20 == EMA50, trend_ok should be False
        assert result.signal is False
