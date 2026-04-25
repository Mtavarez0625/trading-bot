from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indicators import (
    MIN_BARS_REQUIRED,
    add_indicators,
    compute_ema,
    compute_rolling_volume,
    compute_rsi,
)


def _series(*values: float) -> pd.Series:
    return pd.Series(list(values), dtype=float)


def _uptrend(n: int = 60) -> pd.Series:
    return pd.Series([100.0 + i * 0.5 for i in range(n)], dtype=float)


def _downtrend(n: int = 60) -> pd.Series:
    return pd.Series([200.0 - i * 0.5 for i in range(n)], dtype=float)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


class TestComputeEMA:
    def test_returns_series_same_length(self):
        s = _uptrend(30)
        result = compute_ema(s, period=20)
        assert len(result) == len(s)

    def test_insufficient_bars_all_nan(self):
        s = _series(10.0, 20.0)
        result = compute_ema(s, period=5)
        assert result.isna().all()

    def test_constant_series_equals_constant(self):
        s = _series(*[50.0] * 30)
        result = compute_ema(s, period=20)
        assert abs(float(result.iloc[-1]) - 50.0) < 0.01

    def test_uptrend_ema_increases(self):
        s = _uptrend(50)
        result = compute_ema(s, period=20)
        assert float(result.iloc[-1]) > float(result.iloc[25])

    def test_ema20_above_ema50_in_strong_uptrend(self):
        s = _uptrend(70)
        ema20 = compute_ema(s, period=20)
        ema50 = compute_ema(s, period=50)
        assert float(ema20.iloc[-1]) > float(ema50.iloc[-1])

    def test_exact_period_length_does_not_raise(self):
        s = _series(*[10.0] * 20)
        result = compute_ema(s, period=20)
        assert not result.isna().all()


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


class TestComputeRSI:
    def test_returns_series_same_length(self):
        s = _uptrend(30)
        result = compute_rsi(s, period=14)
        assert len(result) == len(s)

    def test_insufficient_bars_all_nan(self):
        s = _series(*[10.0] * 5)
        result = compute_rsi(s, period=14)
        assert result.isna().all()

    def test_strong_uptrend_rsi_above_55(self):
        s = _uptrend(40)
        result = compute_rsi(s, period=14)
        valid = result.dropna()
        assert len(valid) > 0, "Expected valid RSI values with 40 bars"
        assert float(valid.iloc[-1]) > 55.0

    def test_strong_downtrend_rsi_below_45(self):
        s = _downtrend(40)
        result = compute_rsi(s, period=14)
        assert float(result.dropna().iloc[-1]) < 45.0

    def test_rsi_bounded_0_to_100(self):
        import random
        random.seed(0)
        prices = [100.0 + random.uniform(-5, 5) for _ in range(60)]
        s = pd.Series(prices, dtype=float)
        result = compute_rsi(s, period=14)
        valid = result.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_flat_series_does_not_raise(self):
        s = _series(*[50.0] * 30)
        result = compute_rsi(s, period=14)
        # Flat prices → zero gains and losses → RSI may be NaN or 100
        assert len(result) == 30


# ---------------------------------------------------------------------------
# Rolling Volume
# ---------------------------------------------------------------------------


class TestComputeRollingVolume:
    def test_constant_volume_equals_itself(self):
        s = _series(*[1000.0] * 30)
        result = compute_rolling_volume(s, period=20)
        assert abs(float(result.iloc[-1]) - 1000.0) < 0.01

    def test_insufficient_bars_all_nan(self):
        s = _series(*[500.0] * 5)
        result = compute_rolling_volume(s, period=20)
        assert result.isna().all()

    def test_returns_correct_average(self):
        values = list(range(1, 26))  # 1..25, last 20 are 6..25
        s = pd.Series(values, dtype=float)
        result = compute_rolling_volume(s, period=20)
        expected = sum(range(6, 26)) / 20  # = 15.5
        assert abs(float(result.iloc[-1]) - expected) < 0.01


# ---------------------------------------------------------------------------
# add_indicators
# ---------------------------------------------------------------------------


class TestAddIndicators:
    def _make_df(self, n: int = 70) -> pd.DataFrame:
        closes = [100.0 + i * 0.3 for i in range(n)]
        return pd.DataFrame(
            {
                "open": [c - 0.1 for c in closes],
                "high": [c + 0.2 for c in closes],
                "low": [c - 0.2 for c in closes],
                "close": closes,
                "volume": [10000.0 + i * 10 for i in range(n)],
            }
        )

    def test_all_indicator_columns_added(self):
        df = self._make_df()
        result = add_indicators(df)
        for col in ("ema_20", "ema_50", "rsi_14", "vol_avg_20"):
            assert col in result.columns, f"Missing column: {col}"

    def test_original_df_not_mutated(self):
        df = self._make_df()
        original_cols = set(df.columns)
        add_indicators(df)
        assert set(df.columns) == original_cols, "add_indicators must not mutate input"

    def test_indicators_valid_at_last_row_with_enough_bars(self):
        df = self._make_df(n=MIN_BARS_REQUIRED + 10)
        result = add_indicators(df)
        row = result.iloc[-1]
        for col in ("ema_20", "ema_50", "rsi_14", "vol_avg_20"):
            assert not pd.isna(row[col]), f"{col} should not be NaN with enough bars"
