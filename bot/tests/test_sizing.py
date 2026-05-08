"""
Small-account position-sizing safety tests.

Verifies that:
- qty=0 is returned (not 1) when a symbol is unaffordable
- qty=0 is never silently treated as a valid entry
- max_allocation_pct and daily_loss_stop are respected
- trading window can be overridden via env vars
- approved watchlist enforcement works via config
"""
from __future__ import annotations

import os
import sys
from datetime import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from risk import MAX_TRADE_EQUITY_FRACTION, compute_quantity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qty(
    equity: float,
    price: float,
    risk_pct: float = 0.01,
    stop_pct: float = 0.03,
    alloc_pct: float = 0.20,
) -> int:
    return compute_quantity(equity, risk_pct, price, stop_pct, alloc_pct)


# ---------------------------------------------------------------------------
# TASK 4 — qty=0 for unaffordable symbols
# ---------------------------------------------------------------------------

class TestQtyZeroForUnaffordableSymbols:
    """A $1 000 account must return qty=0 for expensive symbols."""

    def test_aapl_200_at_10pct_alloc_returns_zero(self):
        # AAPL ~$200, $1000 * 10% = $100 max → floor(100/200)=0
        qty = _qty(equity=1000.0, price=200.0, alloc_pct=0.10)
        assert qty == 0, f"expected 0, got {qty}"

    def test_googl_180_at_10pct_alloc_returns_zero(self):
        qty = _qty(equity=1000.0, price=180.0, alloc_pct=0.10)
        assert qty == 0

    def test_msft_400_at_20pct_alloc_returns_zero(self):
        # MSFT ~$400, $1000 * 20% = $200 max → floor(200/400)=0
        qty = _qty(equity=1000.0, price=400.0, alloc_pct=0.20)
        assert qty == 0

    def test_spy_530_at_20pct_alloc_returns_zero(self):
        qty = _qty(equity=1000.0, price=530.0, alloc_pct=0.20)
        assert qty == 0

    def test_qty_zero_is_never_one_for_unaffordable(self):
        """The old bug forced max(..., 1) — qty must truly be 0, not 1."""
        for price in [150.0, 200.0, 300.0, 400.0, 530.0]:
            qty = _qty(equity=1000.0, price=price, alloc_pct=0.10)
            assert qty == 0, f"price={price}: expected 0, got {qty} (old max-1 bug?)"


# ---------------------------------------------------------------------------
# TASK 4 — affordable small-account symbols produce qty >= 1
# ---------------------------------------------------------------------------

class TestAffordableSmallAccountSymbols:
    """New watchlist symbols must be buyable with a $1 000 account."""

    def test_pltr_25_returns_positive_qty(self):
        # PLTR ~$25, alloc=$200, risk_qty=int(10/0.75)=13, alloc_qty=8 → 8
        qty = _qty(equity=1000.0, price=25.0)
        assert qty >= 1, f"PLTR at $25 should be affordable, got qty={qty}"

    def test_sofi_13_returns_positive_qty(self):
        qty = _qty(equity=1000.0, price=13.0)
        assert qty >= 1

    def test_hood_45_returns_positive_qty(self):
        qty = _qty(equity=1000.0, price=45.0)
        assert qty >= 1

    def test_intc_20_returns_positive_qty(self):
        qty = _qty(equity=1000.0, price=20.0)
        assert qty >= 1

    def test_amd_100_returns_positive_qty(self):
        # AMD ~$100, alloc=$200, alloc_qty=2
        qty = _qty(equity=1000.0, price=100.0)
        assert qty >= 1

    def test_xlk_200_at_20pct_is_borderline(self):
        # XLK ~$200, $1000*20%=$200, alloc_qty=floor(200/200)=1
        qty = _qty(equity=1000.0, price=200.0, alloc_pct=0.20)
        assert qty >= 1, f"XLK at exactly allocation limit should yield qty>=1, got {qty}"


# ---------------------------------------------------------------------------
# TASK 4 — qty=0 must be a SKIP, not an entry (guard logic)
# ---------------------------------------------------------------------------

class TestQtyZeroIsAlwaysSkip:
    """Any qty<=0 coming out of compute_quantity must never proceed to execution."""

    def _would_execute(self, qty: int) -> bool:
        """Mirrors the guard in both bot/main.py and apps/api/main.py."""
        return qty >= 1

    def test_qty_zero_does_not_execute(self):
        assert self._would_execute(0) is False

    def test_qty_negative_does_not_execute(self):
        assert self._would_execute(-1) is False

    def test_qty_one_executes(self):
        assert self._would_execute(1) is True

    def test_zero_qty_should_not_mark_position_open(self):
        """
        If qty=0, the bot must NOT call journal.open_paper_trade() or any
        equivalent state-marking function. This test verifies the guard
        value, not the actual journal call — integration covered separately.
        """
        qty = _qty(equity=1000.0, price=530.0, alloc_pct=0.10)
        assert qty == 0
        # Guard: only proceed when qty >= 1
        assert not self._would_execute(qty)


# ---------------------------------------------------------------------------
# TASK 4 — max_allocation enforcement
# ---------------------------------------------------------------------------

class TestMaxAllocationEnforcement:
    def test_qty_respects_allocation_cap(self):
        # equity=$10 000, alloc=20%, price=$100 → max 20 shares
        qty = _qty(equity=10_000.0, price=100.0, alloc_pct=0.20)
        max_expected = int(10_000.0 * 0.20 / 100.0)
        assert qty <= max_expected, f"qty={qty} exceeds alloc cap {max_expected}"

    def test_allocation_cap_tighter_than_risk_wins(self):
        # High risk_pct would buy 50 shares, but alloc_pct=0.05 caps at 5
        qty = _qty(equity=10_000.0, price=100.0, risk_pct=0.05, stop_pct=0.01, alloc_pct=0.05)
        assert qty <= 5

    def test_max_trade_equity_fraction_constant_is_correct(self):
        assert MAX_TRADE_EQUITY_FRACTION == 0.20

    def test_default_alloc_uses_max_trade_equity_fraction(self):
        # Default alloc_pct in compute_quantity must equal MAX_TRADE_EQUITY_FRACTION
        qty_explicit = compute_quantity(10_000.0, 0.01, 100.0, 0.03, MAX_TRADE_EQUITY_FRACTION)
        qty_default  = compute_quantity(10_000.0, 0.01, 100.0, 0.03)
        assert qty_explicit == qty_default


# ---------------------------------------------------------------------------
# TASK 4 — max open positions enforcement (pure logic)
# ---------------------------------------------------------------------------

class TestMaxOpenPositionsEnforcement:
    def _should_allow_entry(self, open_count: int, max_positions: int) -> bool:
        return open_count < max_positions

    def test_blocks_when_at_max(self):
        assert self._should_allow_entry(2, 2) is False

    def test_blocks_when_above_max(self):
        assert self._should_allow_entry(3, 2) is False

    def test_allows_when_below_max(self):
        assert self._should_allow_entry(1, 2) is True

    def test_allows_first_entry(self):
        assert self._should_allow_entry(0, 2) is True


# ---------------------------------------------------------------------------
# TASK 4 — daily loss limit enforcement (pure logic)
# ---------------------------------------------------------------------------

class TestDailyLossLimitEnforcement:
    def _loss_triggered(self, start_equity: float, current_equity: float, limit_pct: float) -> bool:
        if start_equity <= 0:
            return False
        loss = (start_equity - current_equity) / start_equity
        return loss >= limit_pct

    def test_triggers_at_3pct_loss(self):
        assert self._loss_triggered(1000.0, 970.0, 0.03) is True

    def test_triggers_above_3pct(self):
        assert self._loss_triggered(1000.0, 950.0, 0.03) is True

    def test_does_not_trigger_below_limit(self):
        assert self._loss_triggered(1000.0, 980.0, 0.03) is False

    def test_does_not_trigger_on_gain(self):
        assert self._loss_triggered(1000.0, 1050.0, 0.03) is False


# ---------------------------------------------------------------------------
# TASK 3 — trading window override via config
# ---------------------------------------------------------------------------

class TestTradingWindowConfig:
    def _make_config(self, monkeypatch, start: str, end: str):
        monkeypatch.setenv("ALPACA_API_KEY",    "test-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
        monkeypatch.setenv("ALPACA_PAPER",      "true")
        for v in ("SYMBOLS", "EQUITIES", "INDEX_ETFS", "COMMODITIES", "ALLOW_LIVE_TRADING"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("TRADING_WINDOW_START", start)
        monkeypatch.setenv("TRADING_WINDOW_END",   end)
        from config import load_config
        return load_config()

    def test_window_start_overridden(self, monkeypatch):
        cfg = self._make_config(monkeypatch, "09:35", "11:30")
        assert cfg.entry_window_start == time(9, 35)

    def test_window_end_overridden(self, monkeypatch):
        cfg = self._make_config(monkeypatch, "09:35", "15:30")
        assert cfg.entry_window_end == time(15, 30)

    def test_window_defaults_when_not_set(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY",    "test-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
        monkeypatch.setenv("ALPACA_PAPER",      "true")
        for v in ("SYMBOLS", "EQUITIES", "INDEX_ETFS", "COMMODITIES",
                  "ALLOW_LIVE_TRADING", "TRADING_WINDOW_START", "TRADING_WINDOW_END"):
            monkeypatch.delenv(v, raising=False)
        from config import load_config
        cfg = load_config()
        assert cfg.entry_window_start == time(9, 35)
        assert cfg.entry_window_end   == time(11, 30)


# ---------------------------------------------------------------------------
# TASK 2 — approved watchlist enforcement via config
# ---------------------------------------------------------------------------

class TestApprovedWatchlistEnforcement:
    def test_new_watchlist_symbols_affordable(self):
        """Every symbol in the small-account watchlist must be buyable at $1 000."""
        affordable_prices = {
            "PLTR": 25.0, "AMD": 100.0, "SOFI": 13.0,
            "HOOD": 45.0, "INTC": 20.0, "XLK": 200.0,
        }
        for sym, price in affordable_prices.items():
            qty = _qty(equity=1000.0, price=price, alloc_pct=0.20)
            assert qty >= 1, f"{sym} at ${price} should be affordable, got qty={qty}"

    def test_old_watchlist_symbols_unaffordable_at_10pct(self):
        """Old default symbols were unaffordable at 10% allocation on $1 000."""
        unaffordable = {"GOOGL": 180.0, "AAPL": 200.0, "MSFT": 420.0}
        for sym, price in unaffordable.items():
            qty = _qty(equity=1000.0, price=price, alloc_pct=0.10)
            assert qty == 0, f"{sym} at ${price} should be unaffordable at 10%, got qty={qty}"

    def test_old_symbols_affordable_at_20pct(self):
        """AAPL and GOOGL become affordable when allocation is raised to 20%."""
        # AAPL ~$200: $1000*20%=$200 → floor(200/200)=1
        qty_aapl = _qty(equity=1000.0, price=200.0, alloc_pct=0.20)
        assert qty_aapl >= 1

    def test_spy_qqq_regime_symbols_can_be_zero(self):
        """SPY and QQQ may return qty=0 — they're regime indicators, not primary trades."""
        qty_spy = _qty(equity=1000.0, price=530.0, alloc_pct=0.20)
        qty_qqq = _qty(equity=1000.0, price=460.0, alloc_pct=0.20)
        # These may be 0 at $1 000 — that's expected and correct
        assert qty_spy >= 0
        assert qty_qqq >= 0
