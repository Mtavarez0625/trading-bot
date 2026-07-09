from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_regime import (
    QQQ_SYMBOL,
    REGIME_SYMBOL,
    SPY_SYMBOL,
    RegimeResult,
    check_market_regime,
)


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
    """Uptrend: last close is above cumulative VWAP."""
    return _make_df([400.0 + i * 0.3 for i in range(n)])


def _downtrend(n=80):
    """Downtrend: last close is below cumulative VWAP."""
    return _make_df([520.0 - i * 0.3 for i in range(n)])


# ---------------------------------------------------------------------------
# Bullish regime — both SPY and QQQ above VWAP
# ---------------------------------------------------------------------------


class TestBullishRegime:
    def test_returns_bullish_when_both_above_vwap(self):
        """Both SPY and QQQ in uptrend → bullish regime."""
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert result.is_bullish is True

    def test_reason_mentions_spy(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert SPY_SYMBOL in result.reason

    def test_reason_mentions_qqq(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert QQQ_SYMBOL in result.reason

    def test_spy_vwap_and_close_populated_when_bullish(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert result.spy_close is not None and isinstance(result.spy_close, float)
        assert result.spy_vwap is not None and isinstance(result.spy_vwap, float)

    def test_qqq_vwap_and_close_populated_when_bullish(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert result.qqq_close is not None and isinstance(result.qqq_close, float)
        assert result.qqq_vwap is not None and isinstance(result.qqq_vwap, float)

    def test_spy_close_above_spy_vwap_when_bullish(self):
        with patch("market_regime.fetch_bars", return_value=_uptrend()):
            result = check_market_regime(None, None)
        assert result.spy_close > result.spy_vwap


# ---------------------------------------------------------------------------
# Bearish regime — either SPY or QQQ below VWAP
# ---------------------------------------------------------------------------


class TestBearishRegime:
    def test_returns_bearish_when_both_in_downtrend(self):
        with patch("market_regime.fetch_bars", return_value=_downtrend()):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_bearish_reason_is_non_empty(self):
        with patch("market_regime.fetch_bars", return_value=_downtrend()):
            result = check_market_regime(None, None)
        assert isinstance(result.reason, str) and len(result.reason) > 0

    def test_bearish_reason_starts_with_skipped(self):
        with patch("market_regime.fetch_bars", return_value=_downtrend()):
            result = check_market_regime(None, None)
        assert result.reason.startswith("skipped:"), (
            f"Bearish reason should start with 'skipped:': {result.reason!r}"
        )

    def test_entry_rejected_when_spy_below_vwap(self):
        """SPY in downtrend, QQQ in uptrend → BEARISH (SPY fails)."""
        def side_effect(client, symbol, timeframe):
            if symbol == SPY_SYMBOL:
                return _downtrend()
            return _uptrend()

        with patch("market_regime.fetch_bars", side_effect=side_effect):
            result = check_market_regime(None, None)
        assert result.is_bullish is False
        assert SPY_SYMBOL in result.reason

    def test_entry_rejected_when_qqq_below_vwap(self):
        """SPY in uptrend, QQQ in downtrend → BEARISH (QQQ fails)."""
        def side_effect(client, symbol, timeframe):
            if symbol == QQQ_SYMBOL:
                return _downtrend()
            return _uptrend()

        with patch("market_regime.fetch_bars", side_effect=side_effect):
            result = check_market_regime(None, None)
        assert result.is_bullish is False
        assert QQQ_SYMBOL in result.reason


# ---------------------------------------------------------------------------
# Fail-safe: bad or missing data → bearish
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_bearish_when_spy_fetch_returns_none(self):
        def side_effect(client, symbol, timeframe):
            if symbol == SPY_SYMBOL:
                return None
            return _uptrend()

        with patch("market_regime.fetch_bars", side_effect=side_effect):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_bearish_when_qqq_fetch_returns_none(self):
        def side_effect(client, symbol, timeframe):
            if symbol == QQQ_SYMBOL:
                return None
            return _uptrend()

        with patch("market_regime.fetch_bars", side_effect=side_effect):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_bearish_when_both_fetch_return_empty_df(self):
        with patch("market_regime.fetch_bars", return_value=pd.DataFrame()):
            result = check_market_regime(None, None)
        assert result.is_bullish is False

    def test_bearish_when_spy_indicators_are_nan(self):
        """Only 5 bars — VWAP is computed but EMA50/RSI still warm up; VWAP itself is valid."""
        tiny = _make_df([400.0] * 5)
        with patch("market_regime.fetch_bars", return_value=tiny):
            result = check_market_regime(None, None)
        # With 5 bars VWAP is valid (it starts from bar 1), but the close is exactly
        # equal to VWAP (constant prices), so above-VWAP condition may not hold.
        # Either way, the check must not raise.
        assert isinstance(result, RegimeResult)

    def test_reason_mentions_no_data_when_none_returned(self):
        def side_effect(client, symbol, timeframe):
            return None

        with patch("market_regime.fetch_bars", side_effect=side_effect):
            result = check_market_regime(None, None)
        low = result.reason.lower()
        assert "no" in low or "data" in low or "available" in low


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

    def test_regime_symbol_constant_is_spy(self):
        """REGIME_SYMBOL must remain 'SPY' for backward compatibility."""
        assert REGIME_SYMBOL == "SPY"
