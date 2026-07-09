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
    compute_relative_volume,
    compute_rolling_volume,
    compute_rsi,
    compute_vwap,
)


def _series(*values: float) -> pd.Series:
    return pd.Series(list(values), dtype=float)


def _uptrend(n: int = 60) -> pd.Series:
    return pd.Series([100.0 + i * 0.5 for i in range(n)], dtype=float)


def _downtrend(n: int = 60) -> pd.Series:
    return pd.Series([200.0 - i * 0.5 for i in range(n)], dtype=float)


def _make_ohlcv(n: int = 70, step: float = 0.3, base_vol: float = 10_000.0) -> pd.DataFrame:
    closes = [100.0 + i * step for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [base_vol + i * 10 for i in range(n)],
        }
    )


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
# VWAP
# ---------------------------------------------------------------------------


class TestComputeVWAP:
    def test_returns_series_same_length(self):
        df = _make_ohlcv(30)
        result = compute_vwap(df)
        assert len(result) == 30

    def test_first_bar_vwap_equals_typical_price(self):
        """VWAP of the very first bar equals its typical price."""
        df = _make_ohlcv(5)
        result = compute_vwap(df)
        tp0 = (df["high"].iloc[0] + df["low"].iloc[0] + df["close"].iloc[0]) / 3.0
        assert abs(float(result.iloc[0]) - tp0) < 1e-6

    def test_constant_ohlcv_vwap_equals_close(self):
        """When all bars are identical, VWAP equals the typical price (= close for symmetric bars)."""
        n = 30
        df = pd.DataFrame(
            {
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [5000.0] * n,
            }
        )
        result = compute_vwap(df)
        tp = (101.0 + 99.0 + 100.0) / 3.0  # = 100.0
        assert abs(float(result.iloc[-1]) - tp) < 1e-6

    def test_uptrend_last_close_above_vwap(self):
        """In a rising market the final close should exceed the cumulative VWAP."""
        df = _make_ohlcv(80, step=0.3)
        result = compute_vwap(df)
        assert df["close"].iloc[-1] > float(result.iloc[-1])

    def test_downtrend_last_close_below_vwap(self):
        """In a falling market the final close should be below the cumulative VWAP."""
        n = 80
        closes = [150.0 - i * 0.3 for i in range(n)]
        df = pd.DataFrame(
            {
                "open": [c + 0.1 for c in closes],
                "high": [c + 0.2 for c in closes],
                "low": [c - 0.2 for c in closes],
                "close": closes,
                "volume": [10_000.0] * n,
            }
        )
        result = compute_vwap(df)
        assert df["close"].iloc[-1] < float(result.iloc[-1])

    def test_no_nan_with_sufficient_data(self):
        df = _make_ohlcv(60)
        result = compute_vwap(df)
        assert not result.isna().any()

    def test_datetimeindex_vwap_resets_per_day(self):
        """VWAP should reset when bars span multiple calendar days."""
        import numpy as np
        from pandas import Timestamp
        # Build two days: 4 bars on day1, 4 bars on day2
        times = (
            [Timestamp("2024-01-02 09:35", tz="UTC") + pd.Timedelta(minutes=5 * i) for i in range(4)]
            + [Timestamp("2024-01-03 09:35", tz="UTC") + pd.Timedelta(minutes=5 * i) for i in range(4)]
        )
        closes = [100.0] * 4 + [110.0] * 4
        df = pd.DataFrame(
            {
                "open": [c - 0.1 for c in closes],
                "high": [c + 0.2 for c in closes],
                "low": [c - 0.2 for c in closes],
                "close": closes,
                "volume": [1_000.0] * 8,
            },
            index=pd.DatetimeIndex(times),
        )
        result = compute_vwap(df)
        # Day 2 VWAP should be ~110, not contaminated by day 1's ~100
        day2_vwap = float(result.iloc[-1])
        assert abs(day2_vwap - 110.0) < 0.5, (
            f"Day-2 VWAP {day2_vwap:.2f} should be near 110 if VWAP resets daily"
        )


# ---------------------------------------------------------------------------
# Relative Volume
# ---------------------------------------------------------------------------


class TestComputeRelativeVolume:
    def test_returns_series_same_length(self):
        vol = pd.Series([1000.0] * 30, dtype=float)
        result = compute_relative_volume(vol, period=20)
        assert len(result) == 30

    def test_constant_volume_rel_vol_is_one(self):
        """When volume is constant, relative volume = 1.0."""
        vol = pd.Series([5000.0] * 30, dtype=float)
        result = compute_relative_volume(vol, period=20)
        assert abs(float(result.iloc[-1]) - 1.0) < 1e-6

    def test_insufficient_bars_are_nan(self):
        """Relative volume requires 20 bars of history; early values should be NaN."""
        vol = pd.Series([1000.0] * 10, dtype=float)
        result = compute_relative_volume(vol, period=20)
        assert result.isna().all()

    def test_spike_produces_high_rel_vol(self):
        """A bar with 3× normal volume should yield relative_volume ≈ 3."""
        baseline = [1000.0] * 30
        baseline[-1] = 3000.0  # spike on last bar
        vol = pd.Series(baseline, dtype=float)
        result = compute_relative_volume(vol, period=20)
        # Average of bars -21 to -2: all are 1000; last bar is 3000 → rel_vol ≈ 3
        assert float(result.iloc[-1]) >= 2.5

    def test_rel_vol_above_two_meets_threshold(self):
        """Verify the REL_VOL_MIN=2.0 threshold is reachable with a clear volume spike."""
        from strategy import REL_VOL_MIN
        baseline = [1000.0] * 30
        baseline[-1] = 2500.0
        vol = pd.Series(baseline, dtype=float)
        result = compute_relative_volume(vol, period=20)
        assert float(result.iloc[-1]) >= REL_VOL_MIN


# ---------------------------------------------------------------------------
# add_indicators
# ---------------------------------------------------------------------------


class TestAddIndicators:
    def test_all_indicator_columns_added(self):
        df = _make_ohlcv()
        result = add_indicators(df)
        for col in ("ema_20", "ema_50", "rsi_14", "vol_avg_20", "vwap", "relative_volume"):
            assert col in result.columns, f"Missing column: {col}"

    def test_original_df_not_mutated(self):
        df = _make_ohlcv()
        original_cols = set(df.columns)
        add_indicators(df)
        assert set(df.columns) == original_cols, "add_indicators must not mutate input"

    def test_indicators_valid_at_last_row_with_enough_bars(self):
        df = _make_ohlcv(n=MIN_BARS_REQUIRED + 10)
        result = add_indicators(df)
        row = result.iloc[-1]
        for col in ("ema_20", "ema_50", "rsi_14", "vol_avg_20", "vwap", "relative_volume"):
            assert not pd.isna(row[col]), f"{col} should not be NaN with enough bars"
