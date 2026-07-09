"""
Tests for Hybrid Ross Momentum Strategy safety guards:
  - max trades per symbol enforcement
  - daily loss stop enforcement

These guards live in main.py / run_cycle; here we test the underlying
state and calculation logic in isolation.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from state import TradingState


# ---------------------------------------------------------------------------
# Max trades per symbol
# ---------------------------------------------------------------------------


class TestMaxTradesPerSymbol:
    """
    The guard in _evaluate_symbol checks:
        state.get_trade_count(symbol) >= config.max_trades_per_symbol
    We test TradingState directly.
    """

    def test_initial_trade_count_is_zero(self):
        state = TradingState()
        assert state.get_trade_count("SOFI") == 0

    def test_increment_returns_new_count(self):
        state = TradingState()
        count = state.increment_trade_count("SOFI")
        assert count == 1

    def test_limit_detected_after_max_trades(self):
        max_trades = 1
        state = TradingState()
        state.increment_trade_count("SOFI")
        assert state.get_trade_count("SOFI") >= max_trades

    def test_different_symbols_tracked_independently(self):
        state = TradingState()
        state.increment_trade_count("SOFI")
        state.increment_trade_count("SOFI")
        state.increment_trade_count("BAC")
        assert state.get_trade_count("SOFI") == 2
        assert state.get_trade_count("BAC") == 1
        assert state.get_trade_count("PLTR") == 0

    def test_counts_reset_on_new_day(self):
        """Trade counts must reset when the calendar date changes."""
        from datetime import date
        state = TradingState()
        state.increment_trade_count("SOFI")
        assert state.get_trade_count("SOFI") == 1

        # Force the state's internal date to yesterday so reset fires
        yesterday = date.fromordinal(date.today().toordinal() - 1)
        state._trade_date = yesterday  # access internal attr to simulate day rollover
        state.reset_if_new_day()
        assert state.get_trade_count("SOFI") == 0, (
            "Trade count should reset to 0 after a day rollover"
        )

    def test_max_trades_per_symbol_is_1_in_env(self):
        """Confirm .env sets MAX_TRADES_PER_SYMBOL=1 for the Hybrid strategy."""
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if not os.path.exists(env_path):
            pytest.skip(".env not found — skipping env validation")
        with open(env_path) as f:
            content = f.read()
        assert "MAX_TRADES_PER_SYMBOL=1" in content, (
            "Hybrid strategy requires MAX_TRADES_PER_SYMBOL=1 in .env"
        )


# ---------------------------------------------------------------------------
# Daily loss stop
# ---------------------------------------------------------------------------


class TestDailyLossStop:
    """
    The guard in run_cycle checks:
        (start_equity - equity) / start_equity >= daily_loss_stop
    We test the math and TradingState's equity tracking directly.
    """

    def _loss_pct(self, start: float, current: float) -> float:
        return (start - current) / start

    def test_no_loss_stop_when_equity_flat(self):
        start = 1000.0
        current = 1000.0
        assert self._loss_pct(start, current) == 0.0

    def test_loss_pct_calculation_correct(self):
        start = 1000.0
        current = 970.0
        assert abs(self._loss_pct(start, current) - 0.03) < 1e-9

    def test_stop_triggered_at_daily_loss_limit(self):
        daily_loss_stop = 0.03
        start = 1000.0
        current = 970.0  # exactly 3% down
        assert self._loss_pct(start, current) >= daily_loss_stop

    def test_stop_not_triggered_below_daily_loss_limit(self):
        daily_loss_stop = 0.03
        start = 1000.0
        current = 975.0  # only 2.5% down
        assert self._loss_pct(start, current) < daily_loss_stop

    def test_start_equity_stored_in_state(self):
        state = TradingState()
        state.set_start_equity(1000.0)
        assert state.get_start_equity() == 1000.0

    def test_start_equity_only_set_once_per_day(self):
        """Second call to set_start_equity on the same day should be ignored."""
        state = TradingState()
        state.set_start_equity(1000.0)
        state.set_start_equity(950.0)  # should not overwrite
        assert state.get_start_equity() == 1000.0, (
            "Start equity must only be set once per day (first call wins)"
        )

    def test_daily_loss_stop_value_in_env(self):
        """Confirm .env sets DAILY_LOSS_STOP=0.03 for the Hybrid strategy."""
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if not os.path.exists(env_path):
            pytest.skip(".env not found — skipping env validation")
        with open(env_path) as f:
            content = f.read()
        assert "DAILY_LOSS_STOP=0.03" in content, (
            "Hybrid strategy requires DAILY_LOSS_STOP=0.03 in .env"
        )
