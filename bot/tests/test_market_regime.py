from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_regime import REGIME_SYMBOL, RegimeResult, check_market_regime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(closes, volumes=None):
    """Build a minimal OHLCV DataFrame suitable for indicator computation."""
    n = len(closes)
    vols = volumes or [10_000.0 + i * 100 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.3 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": vols,
        }
    )


def _uptrend(n=80):
    return _make_df([400.0 + i * 0.6 for i in range(n)])


def _downtrend(n=80):
    return _make_df([520.0 - i * 0.6 for i in range(n)])


# ---------------------------------------------------------------------------
# Bullish regime
# ---------------------------------------------------------------------------


class TestBullishRegime:
    def test_returns_bullish_on_uptrend(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert result.is_bullish is True

    def test_reason_mentions_spy(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert REGIME_SYMBOL in result.reason

    def test_indicator_values_populated_when_bullish(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert result.ema_20 is not None
        assert result.ema_50 is not None
        assert result.rsi_14 is not None
        assert isinstance(result.ema_20, float)
        assert isinstance(result.ema_50, float)
        assert isinstance(result.rsi_14, float)

    def test_ema20_above_ema50_when_bullish(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert result.ema_20 > result.ema_50


# ---------------------------------------------------------------------------
# Bearish regime
# ---------------------------------------------------------------------------


class TestBearishRegime:
    def test_returns_bearish_on_downtrend(self):
        with patch("market_regime.fetch_bars", return_value=_downtrend()):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_bearish_reason_is_non_empty(self):
        with patch("market_regime.fetch_bars", return_value=_downtrend()):
            result = check_market_regime(None, None)
        assert isinstance(result.reason, str) and len(result.reason) > 0

    def test_bearish_reason_mentions_spy(self):
        with patch("market_regime.fetch_bars", return_value=_downtrend()):
            result = check_market_regime(None, None)
        assert REGIME_SYMBOL in result.reason


# ---------------------------------------------------------------------------
# Fail-safe: bad or missing data → bearish
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_bearish_when_fetch_returns_none(self):
        with patch("market_regime.fetch_bars", return_value=None):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_bearish_when_fetch_returns_empty_df(self):
        with patch("market_regime.fetch_bars", return_value=pd.DataFrame()):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_bearish_when_indicators_are_nan(self):
        # Only 5 bars — not enough for EMA50/RSI to compute; all indicators → NaN
        tiny_df = _make_df([400.0] * 5)
        with patch("market_regime.fetch_bars", return_value=tiny_df):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_reason_mentions_no_data(self):
        with patch("market_regime.fetch_bars", return_value=None):
            result = check_market_regime(None, None)
        assert "No" in result.reason or "available" in result.reason or "data" in result.reason.lower()


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------


class TestReturnType:
    def test_always_returns_regime_result(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert isinstance(result, RegimeResult)

    def test_is_bullish_is_bool(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert isinstance(result.is_bullish, bool)

    def test_reason_is_str(self):
        with patch("market_regime.fetch_bars", return_value=_downtrend()):
            result = check_market_regime(None, None)
        assert isinstance(result.reason, str)
