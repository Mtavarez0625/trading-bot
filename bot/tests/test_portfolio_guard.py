from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from portfolio_guard import (
    count_group_positions,
    count_open_positions,
    get_open_position_symbols,
    has_open_orders,
    has_open_position,
    is_account_tradable,
    is_asset_tradable,
)


# ---------------------------------------------------------------------------
# Minimal fake clients
# ---------------------------------------------------------------------------


class _FakePosition:
    def __init__(self, symbol: str, qty: float = 10.0):
        self.symbol = symbol
        self.qty = str(qty)  # Alpaca returns strings


class _FakeAsset:
    def __init__(self, tradable: bool = True, status: str = "active"):
        self.tradable = tradable
        self.status = status


class _FakeAccount:
    def __init__(
        self,
        status: str = "active",
        trading_blocked: bool = False,
        account_blocked: bool = False,
        equity: str = "10000.00",
    ):
        self.status = status
        self.trading_blocked = trading_blocked
        self.account_blocked = account_blocked
        self.equity = equity
        self.buying_power = "20000.00"


class _FakeOrder:
    pass


# ---------------------------------------------------------------------------
# get_open_position_symbols
# ---------------------------------------------------------------------------


class TestGetOpenPositionSymbols:
    def test_returns_set_of_symbols(self):
        class Client:
            def get_all_positions(self):
                return [_FakePosition("AAPL"), _FakePosition("MSFT")]

        result = get_open_position_symbols(Client())
        assert result == {"AAPL", "MSFT"}

    def test_returns_empty_set_when_no_positions(self):
        class Client:
            def get_all_positions(self):
                return []

        assert get_open_position_symbols(Client()) == set()

    def test_returns_empty_set_on_api_error(self):
        class Client:
            def get_all_positions(self):
                raise ConnectionError("timeout")

        assert get_open_position_symbols(Client()) == set()

    def test_symbols_are_uppercase(self):
        class FakePos:
            symbol = "aapl"

        class Client:
            def get_all_positions(self):
                return [FakePos()]

        result = get_open_position_symbols(Client())
        assert "AAPL" in result

    def test_filters_positions_without_symbol(self):
        class FakePosNoSymbol:
            symbol = ""

        class Client:
            def get_all_positions(self):
                return [FakePosNoSymbol(), _FakePosition("NVDA")]

        result = get_open_position_symbols(Client())
        assert result == {"NVDA"}

    def test_returns_set_type(self):
        class Client:
            def get_all_positions(self):
                return [_FakePosition("SPY")]

        assert isinstance(get_open_position_symbols(Client()), set)


# ---------------------------------------------------------------------------
# count_open_positions
# ---------------------------------------------------------------------------


class TestCountOpenPositions:
    def test_returns_correct_count(self):
        class Client:
            def get_all_positions(self):
                return [_FakePosition("AAPL"), _FakePosition("MSFT"), _FakePosition("NVDA")]

        assert count_open_positions(Client()) == 3

    def test_returns_zero_on_empty(self):
        class Client:
            def get_all_positions(self):
                return []

        assert count_open_positions(Client()) == 0

    def test_returns_zero_on_api_error(self):
        class Client:
            def get_all_positions(self):
                raise RuntimeError("API down")

        assert count_open_positions(Client()) == 0


# ---------------------------------------------------------------------------
# has_open_position
# ---------------------------------------------------------------------------


class TestHasOpenPosition:
    def test_true_when_position_exists(self):
        class Client:
            def get_open_position(self, symbol):
                return _FakePosition(symbol, qty=5.0)

        assert has_open_position(Client(), "AAPL") is True

    def test_false_when_404(self):
        class Client:
            def get_open_position(self, symbol):
                raise Exception("position does not exist")

        assert has_open_position(Client(), "AAPL") is False

    def test_false_when_qty_zero(self):
        class Client:
            def get_open_position(self, symbol):
                return _FakePosition(symbol, qty=0.0)

        assert has_open_position(Client(), "AAPL") is False


# ---------------------------------------------------------------------------
# has_open_orders
# ---------------------------------------------------------------------------


class TestHasOpenOrders:
    def test_true_when_orders_exist(self):
        class Client:
            def get_orders(self, request):
                return [_FakeOrder(), _FakeOrder()]

        assert has_open_orders(Client(), "AAPL") is True

    def test_false_when_no_orders(self):
        class Client:
            def get_orders(self, request):
                return []

        assert has_open_orders(Client(), "AAPL") is False

    def test_false_on_api_error(self):
        class Client:
            def get_orders(self, request):
                raise RuntimeError("API error")

        assert has_open_orders(Client(), "AAPL") is False


# ---------------------------------------------------------------------------
# is_asset_tradable
# ---------------------------------------------------------------------------


class TestIsAssetTradable:
    def test_true_for_active_tradable_asset(self):
        class Client:
            def get_asset(self, symbol):
                return _FakeAsset(tradable=True, status="active")

        assert is_asset_tradable(Client(), "AAPL") is True

    def test_false_for_inactive_asset(self):
        class Client:
            def get_asset(self, symbol):
                return _FakeAsset(tradable=True, status="inactive")

        assert is_asset_tradable(Client(), "AAPL") is False

    def test_false_for_non_tradable_asset(self):
        class Client:
            def get_asset(self, symbol):
                return _FakeAsset(tradable=False, status="active")

        assert is_asset_tradable(Client(), "AAPL") is False

    def test_false_on_api_error(self):
        class Client:
            def get_asset(self, symbol):
                raise Exception("Not found")

        assert is_asset_tradable(Client(), "XYZW") is False


# ---------------------------------------------------------------------------
# is_account_tradable
# ---------------------------------------------------------------------------


class TestIsAccountTradable:
    def test_returns_true_and_equity_for_good_account(self):
        class Client:
            def get_account(self):
                return _FakeAccount()

        tradable, equity = is_account_tradable(Client())
        assert tradable is True
        assert equity == 10000.0

    def test_false_when_account_not_active(self):
        class Client:
            def get_account(self):
                return _FakeAccount(status="suspended")

        tradable, equity = is_account_tradable(Client())
        assert tradable is False
        assert equity == 0.0

    def test_false_when_trading_blocked(self):
        class Client:
            def get_account(self):
                return _FakeAccount(trading_blocked=True)

        tradable, equity = is_account_tradable(Client())
        assert tradable is False

    def test_false_when_account_blocked(self):
        class Client:
            def get_account(self):
                return _FakeAccount(account_blocked=True)

        tradable, equity = is_account_tradable(Client())
        assert tradable is False

    def test_false_on_api_error(self):
        class Client:
            def get_account(self):
                raise ConnectionError("network error")

        tradable, equity = is_account_tradable(Client())
        assert tradable is False
        assert equity == 0.0

    def test_false_when_equity_zero(self):
        class Client:
            def get_account(self):
                return _FakeAccount(equity="0.00")

        tradable, equity = is_account_tradable(Client())
        assert tradable is False


# ---------------------------------------------------------------------------
# Per-symbol max-positions logic (pure, no broker calls)
# ---------------------------------------------------------------------------


class TestMaxPositionsLogic:
    """
    Verifies the open_count_ref pattern used in main._evaluate_symbol.
    This tests the logic in isolation, not the full cycle.
    """

    def _should_skip(self, open_count: int, max_positions: int) -> bool:
        """Mirrors the guard in _evaluate_symbol."""
        return open_count >= max_positions

    def test_skip_when_at_max(self):
        assert self._should_skip(3, 3) is True

    def test_skip_when_above_max(self):
        assert self._should_skip(4, 3) is True

    def test_no_skip_when_below_max(self):
        assert self._should_skip(2, 3) is False

    def test_no_skip_when_zero_positions(self):
        assert self._should_skip(0, 3) is False

    def test_no_skip_when_max_is_one_and_zero_open(self):
        assert self._should_skip(0, 1) is False

    def test_skip_when_max_is_one_and_one_open(self):
        assert self._should_skip(1, 1) is True

    def test_open_count_ref_increments_correctly(self):
        """Simulate the cycle: each execution increments the ref."""
        max_positions = 3
        open_count_ref = [1]  # 1 already open

        skipped = []
        executed = []

        for symbol in ["AAPL", "MSFT", "NVDA", "TSLA"]:
            if open_count_ref[0] >= max_positions:
                skipped.append(symbol)
            else:
                # Simulate trade execution
                open_count_ref[0] += 1
                executed.append(symbol)

        assert len(executed) == 2    # started at 1, cap at 3 → 2 more
        assert len(skipped) == 2     # NVDA and TSLA blocked
        assert "AAPL" in executed
        assert "MSFT" in executed
        assert "NVDA" in skipped
        assert "TSLA" in skipped


# ---------------------------------------------------------------------------
# count_group_positions
# ---------------------------------------------------------------------------


class TestCountGroupPositions:
    def test_counts_open_symbol_from_group(self):
        class Client:
            def get_all_positions(self):
                return [_FakePosition("SPY"), _FakePosition("AAPL")]

        assert count_group_positions(Client(), {"SPY", "QQQ"}) == 1

    def test_counts_both_when_both_open(self):
        class Client:
            def get_all_positions(self):
                return [_FakePosition("SPY"), _FakePosition("QQQ")]

        assert count_group_positions(Client(), {"SPY", "QQQ"}) == 2

    def test_zero_when_no_group_member_open(self):
        class Client:
            def get_all_positions(self):
                return [_FakePosition("AAPL"), _FakePosition("MSFT")]

        assert count_group_positions(Client(), {"SPY", "QQQ"}) == 0

    def test_zero_when_no_positions_at_all(self):
        class Client:
            def get_all_positions(self):
                return []

        assert count_group_positions(Client(), {"SPY", "QQQ"}) == 0

    def test_zero_on_api_error(self):
        class Client:
            def get_all_positions(self):
                raise RuntimeError("API timeout")

        assert count_group_positions(Client(), {"SPY", "QQQ"}) == 0

    def test_case_insensitive_match(self):
        class FakePosLower:
            symbol = "spy"
            qty = "10"

        class Client:
            def get_all_positions(self):
                return [FakePosLower()]

        assert count_group_positions(Client(), {"SPY", "QQQ"}) == 1

    def test_empty_group_always_zero(self):
        class Client:
            def get_all_positions(self):
                return [_FakePosition("SPY")]

        assert count_group_positions(Client(), set()) == 0


# ---------------------------------------------------------------------------
# Correlated guard logic (pure — mirrors _evaluate_symbol in main.py)
# ---------------------------------------------------------------------------


class TestCorrelatedGroupGuard:
    """
    Verifies the ETF correlated-group guard logic used in main._evaluate_symbol.
    Tests the pure calculation in isolation (no broker calls).
    """

    @staticmethod
    def _should_skip(open_syms: set, group: set, max_group: int) -> bool:
        """Mirrors: count_group_positions(...) >= config.max_etf_group_positions."""
        count = sum(1 for s in group if s.upper() in {x.upper() for x in open_syms})
        return count >= max_group

    def test_skip_qqq_when_spy_open(self):
        assert self._should_skip({"SPY"}, {"SPY", "QQQ"}, max_group=1) is True

    def test_skip_spy_when_qqq_open(self):
        assert self._should_skip({"QQQ"}, {"SPY", "QQQ"}, max_group=1) is True

    def test_allow_entry_when_group_empty(self):
        assert self._should_skip({"AAPL", "MSFT"}, {"SPY", "QQQ"}, max_group=1) is False

    def test_allow_when_no_positions_at_all(self):
        assert self._should_skip(set(), {"SPY", "QQQ"}, max_group=1) is False

    def test_allow_second_etf_when_max_is_2(self):
        assert self._should_skip({"SPY"}, {"SPY", "QQQ"}, max_group=2) is False

    def test_skip_when_both_open_and_max_is_2(self):
        assert self._should_skip({"SPY", "QQQ"}, {"SPY", "QQQ"}, max_group=2) is True

    def test_equity_symbol_not_blocked_by_etf_guard(self):
        # AAPL is not in the ETF group — check logic never fires for it
        group = {"SPY", "QQQ"}
        assert "AAPL" not in group  # guard should not apply to AAPL
