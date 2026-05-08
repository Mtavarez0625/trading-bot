from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from exit_manager import (
    BREAK_EVEN_TRIGGER_PCT,
    TRAILING_STOP_TRAIL_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    attempt_stop_modification,
    calc_trailing_stop_price,
    calc_unrealized_pct,
    monitor_and_manage_exits,
    should_apply_break_even,
    should_apply_trailing_stop,
)


# ---------------------------------------------------------------------------
# calc_unrealized_pct
# ---------------------------------------------------------------------------


class TestCalcUnrealizedPct:
    def test_gain(self):
        result = calc_unrealized_pct(100.0, 101.0)
        assert abs(result - 0.01) < 1e-9

    def test_loss(self):
        result = calc_unrealized_pct(100.0, 99.0)
        assert abs(result - (-0.01)) < 1e-9

    def test_break_even_returns_zero(self):
        assert calc_unrealized_pct(100.0, 100.0) == 0.0

    def test_zero_entry_returns_zero(self):
        assert calc_unrealized_pct(0.0, 150.0) == 0.0

    def test_negative_entry_returns_zero(self):
        assert calc_unrealized_pct(-10.0, 150.0) == 0.0

    def test_large_gain(self):
        result = calc_unrealized_pct(200.0, 220.0)
        assert abs(result - 0.10) < 1e-9


# ---------------------------------------------------------------------------
# should_apply_break_even
# ---------------------------------------------------------------------------


class TestShouldApplyBreakEven:
    def test_triggers_exactly_at_threshold(self):
        assert should_apply_break_even(BREAK_EVEN_TRIGGER_PCT) is True

    def test_triggers_above_threshold(self):
        assert should_apply_break_even(BREAK_EVEN_TRIGGER_PCT + 0.001) is True

    def test_does_not_trigger_just_below_threshold(self):
        assert should_apply_break_even(BREAK_EVEN_TRIGGER_PCT - 0.0001) is False

    def test_does_not_trigger_at_zero(self):
        assert should_apply_break_even(0.0) is False

    def test_does_not_trigger_on_loss(self):
        assert should_apply_break_even(-0.05) is False

    def test_threshold_is_one_percent(self):
        assert BREAK_EVEN_TRIGGER_PCT == 0.010


# ---------------------------------------------------------------------------
# should_apply_trailing_stop
# ---------------------------------------------------------------------------


class TestShouldApplyTrailingStop:
    def test_triggers_exactly_at_threshold(self):
        assert should_apply_trailing_stop(TRAILING_STOP_TRIGGER_PCT) is True

    def test_triggers_above_threshold(self):
        assert should_apply_trailing_stop(TRAILING_STOP_TRIGGER_PCT + 0.001) is True

    def test_does_not_trigger_just_below_threshold(self):
        assert should_apply_trailing_stop(TRAILING_STOP_TRIGGER_PCT - 0.0001) is False

    def test_does_not_trigger_at_break_even_level(self):
        # +1.0 % is above break-even threshold but below trailing-stop threshold
        assert should_apply_trailing_stop(BREAK_EVEN_TRIGGER_PCT) is False

    def test_does_not_trigger_on_loss(self):
        assert should_apply_trailing_stop(-0.02) is False

    def test_threshold_is_one_point_five_percent(self):
        assert TRAILING_STOP_TRIGGER_PCT == 0.015

    def test_trailing_higher_than_break_even(self):
        assert TRAILING_STOP_TRIGGER_PCT > BREAK_EVEN_TRIGGER_PCT


# ---------------------------------------------------------------------------
# calc_trailing_stop_price
# ---------------------------------------------------------------------------


class TestCalcTrailingStopPrice:
    def test_basic(self):
        # current=100.00, trail=0.75% → 100 × (1 - 0.0075) = 99.25
        result = calc_trailing_stop_price(100.0)
        assert result == 99.25

    def test_scales_with_price(self):
        result = calc_trailing_stop_price(200.0)
        assert abs(result - 198.50) < 0.001

    def test_rounded_to_two_decimal_places(self):
        result = calc_trailing_stop_price(157.33)
        assert result == round(157.33 * (1 - TRAILING_STOP_TRAIL_PCT), 2)

    def test_trail_pct_is_conservative(self):
        # Trail must be <= 1% (conservative per spec)
        assert TRAILING_STOP_TRAIL_PCT <= 0.01

    def test_stop_is_below_current_price(self):
        current = 150.0
        assert calc_trailing_stop_price(current) < current


# ---------------------------------------------------------------------------
# attempt_stop_modification — log-only path (no order ID)
# ---------------------------------------------------------------------------


class TestAttemptStopModification:
    def test_returns_false_when_no_order_id(self):
        class FakeClient:
            pass

        result = attempt_stop_modification(
            FakeClient(), None, 99.50, "AAPL", "break-even"
        )
        assert result is False

    def test_returns_false_on_api_error(self):
        class FakeClient:
            def replace_order_by_id(self, order_id, req):
                raise RuntimeError("API failure")

        result = attempt_stop_modification(
            FakeClient(), "fake-order-id", 99.50, "AAPL", "trailing-stop"
        )
        assert result is False

    def test_returns_true_on_successful_replace(self):
        class FakeClient:
            def replace_order_by_id(self, order_id, req):
                pass  # no error = success

        result = attempt_stop_modification(
            FakeClient(), "fake-order-id", 99.50, "MSFT", "break-even"
        )
        assert result is True


# ---------------------------------------------------------------------------
# monitor_and_manage_exits — integration-level with fake client
# ---------------------------------------------------------------------------


class _FakePosition:
    def __init__(self, symbol, qty, avg_entry_price, current_price):
        self.symbol = symbol
        self.qty = str(qty)
        self.avg_entry_price = str(avg_entry_price)
        self.current_price = str(current_price)


class TestMonitorAndManageExits:
    def test_no_positions_runs_without_error(self):
        class Client:
            def get_all_positions(self):
                return []

        monitor_and_manage_exits(Client())  # must not raise

    def test_api_error_runs_without_error(self):
        class Client:
            def get_all_positions(self):
                raise ConnectionError("timeout")

        monitor_and_manage_exits(Client())  # must not raise

    def test_break_even_threshold_position(self):
        # entry=100, current=101.5 → +1.5 % → triggers trailing stop (higher tier)
        class Client:
            def get_all_positions(self):
                return [_FakePosition("AAPL", 10, 100.0, 101.5)]

        monitor_and_manage_exits(Client())  # must not raise

    def test_flat_position_no_action(self):
        # entry=100, current=100 → 0 % → no threshold met
        class Client:
            def get_all_positions(self):
                return [_FakePosition("MSFT", 5, 100.0, 100.0)]

        monitor_and_manage_exits(Client())  # must not raise

    def test_losing_position_no_action(self):
        # entry=100, current=98 → -2 % → no threshold
        class Client:
            def get_all_positions(self):
                return [_FakePosition("NVDA", 3, 100.0, 98.0)]

        monitor_and_manage_exits(Client())  # must not raise

    def test_multiple_positions(self):
        class Client:
            def get_all_positions(self):
                return [
                    _FakePosition("AAPL", 10, 100.0, 101.5),   # trailing stop tier
                    _FakePosition("MSFT", 5, 200.0, 202.2),    # break-even tier
                    _FakePosition("NVDA", 3, 300.0, 298.0),    # loss — no action
                ]

        monitor_and_manage_exits(Client())  # must not raise

    def test_break_even_tier_exactly_at_one_pct(self):
        # entry=100, current=101 → exactly +1.0 % → break-even (not trailing)
        pct = calc_unrealized_pct(100.0, 101.0)
        assert should_apply_break_even(pct) is True
        assert should_apply_trailing_stop(pct) is False

    def test_trailing_tier_at_one_point_five_pct(self):
        # entry=100, current=101.5 → exactly +1.5 % → trailing stop
        pct = calc_unrealized_pct(100.0, 101.5)
        assert should_apply_trailing_stop(pct) is True
