from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from risk import (
    MAX_TRADE_EQUITY_FRACTION,
    compute_quantity,
    compute_stop_price,
    compute_take_profit_price,
)


# ---------------------------------------------------------------------------
# compute_stop_price
# ---------------------------------------------------------------------------


class TestComputeStopPrice:
    def test_one_percent(self):
        assert compute_stop_price(100.0, 0.01) == 99.0

    def test_two_percent(self):
        assert compute_stop_price(200.0, 0.02) == 196.0

    def test_rounding(self):
        result = compute_stop_price(152.37, 0.01)
        assert result == round(152.37 * 0.99, 2)

    def test_result_less_than_entry(self):
        entry = 350.00
        stop = compute_stop_price(entry, 0.01)
        assert stop < entry


# ---------------------------------------------------------------------------
# compute_take_profit_price
# ---------------------------------------------------------------------------


class TestComputeTakeProfitPrice:
    def test_two_percent(self):
        assert compute_take_profit_price(100.0, 0.02) == 102.0

    def test_five_percent(self):
        assert compute_take_profit_price(200.0, 0.05) == 210.0

    def test_rounding(self):
        result = compute_take_profit_price(152.37, 0.02)
        assert result == round(152.37 * 1.02, 2)

    def test_result_greater_than_entry(self):
        entry = 350.00
        tp = compute_take_profit_price(entry, 0.02)
        assert tp > entry


# ---------------------------------------------------------------------------
# compute_quantity
# ---------------------------------------------------------------------------


class TestComputeQuantity:
    def test_standard_case(self):
        # equity=10_000, risk=1%, entry=100, stop=1%
        # risk_amount=100, stop_dist=1.0, raw_qty=100
        # BUT 20%-equity cap = floor(10_000*0.20/100) = 20, so qty=20
        qty = compute_quantity(10_000.0, 0.01, 100.0, 0.01)
        assert qty == 20

    def test_risk_formula_without_cap(self):
        # With a large equity the cap does not bind and risk formula dominates.
        # equity=1_000_000, risk=0.001% (0.00001), entry=100, stop=1%
        # risk_amount=10, stop_dist=1.0, raw_qty=10
        # max_affordable=floor(1_000_000*0.20/100)=2000 — cap does not bind
        qty = compute_quantity(1_000_000.0, 0.00001, 100.0, 0.01)
        assert qty == 10

    def test_returns_integer(self):
        qty = compute_quantity(50_000.0, 0.01, 175.50, 0.01)
        assert isinstance(qty, int)

    def test_zero_equity(self):
        assert compute_quantity(0.0, 0.01, 100.0, 0.01) == 0

    def test_negative_equity(self):
        assert compute_quantity(-1000.0, 0.01, 100.0, 0.01) == 0

    def test_zero_entry_price(self):
        assert compute_quantity(10_000.0, 0.01, 0.0, 0.01) == 0

    def test_negative_entry_price(self):
        assert compute_quantity(10_000.0, 0.01, -50.0, 0.01) == 0

    def test_zero_stop_loss_pct(self):
        assert compute_quantity(10_000.0, 0.01, 100.0, 0.0) == 0

    def test_zero_risk_per_trade(self):
        assert compute_quantity(10_000.0, 0.0, 100.0, 0.01) == 0

    def test_floors_to_zero_when_too_small(self):
        # risk_amount=1, stop_distance=10 → raw=0.1 → floor=0
        qty = compute_quantity(100.0, 0.01, 100.0, 0.10)
        assert qty == 0

    def test_capped_at_max_equity_fraction(self):
        # With very high risk_per_trade, qty should never exceed 20% equity / price
        # equity=10_000, entry=1.0 → max_affordable = floor(2000/1.0) = 2000
        qty = compute_quantity(10_000.0, 0.50, 1.0, 0.01)
        max_expected = int((10_000.0 * MAX_TRADE_EQUITY_FRACTION) / 1.0)
        assert qty <= max_expected

    def test_result_is_non_negative(self):
        qty = compute_quantity(500.0, 0.01, 400.0, 0.01)
        assert qty >= 0

    def test_scales_with_equity(self):
        qty_small = compute_quantity(10_000.0, 0.01, 100.0, 0.01)
        qty_large = compute_quantity(100_000.0, 0.01, 100.0, 0.01)
        assert qty_large > qty_small
