from __future__ import annotations

from typing import Set, Tuple

from logger import get_logger

log = get_logger(__name__)


def get_account(trading_client):
    """Fetch account object. Returns None on any failure."""
    try:
        return trading_client.get_account()
    except Exception as exc:
        log.error("Failed to fetch account: %s", exc)
        return None


def is_account_tradable(trading_client) -> Tuple[bool, float]:
    """
    Verify the account is active, unblocked, and has positive equity.

    Returns:
        (tradable, equity) — equity is 0.0 when tradable is False.
    """
    account = get_account(trading_client)
    if account is None:
        return False, 0.0

    status = getattr(account, "status", "").lower()
    if status != "active":
        log.warning("Account status is '%s' — not tradable.", status)
        return False, 0.0

    if getattr(account, "trading_blocked", True):
        log.warning("Account trading is blocked.")
        return False, 0.0

    if getattr(account, "account_blocked", True):
        log.warning("Account is blocked.")
        return False, 0.0

    try:
        equity = float(account.equity)
    except (TypeError, ValueError) as exc:
        log.error("Could not parse equity from account: %s", exc)
        return False, 0.0

    if equity <= 0:
        log.warning("Account equity is non-positive: %.2f", equity)
        return False, 0.0

    return True, equity


def has_open_position(trading_client, symbol: str) -> bool:
    """Return True if there is a non-zero position for `symbol`."""
    try:
        position = trading_client.get_open_position(symbol)
        qty = float(getattr(position, "qty", 0))
        return qty != 0.0
    except Exception as exc:
        exc_str = str(exc).lower()
        if "position does not exist" in exc_str or "404" in exc_str:
            return False
        log.warning("[%s] Unexpected error checking position: %s", symbol, exc)
        return False


def has_open_orders(trading_client, symbol: str) -> bool:
    """Return True if there are any open orders for `symbol`."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol],
        )
        orders = trading_client.get_orders(request)
        return len(orders) > 0
    except Exception as exc:
        log.warning("[%s] Unexpected error checking open orders: %s", symbol, exc)
        return False


def is_asset_tradable(trading_client, symbol: str) -> bool:
    """Return True if the asset exists, is active, and is tradable on Alpaca."""
    try:
        asset = trading_client.get_asset(symbol)
        tradable = getattr(asset, "tradable", False)
        status = getattr(asset, "status", "").lower()
        if not tradable or status != "active":
            log.warning(
                "[%s] Asset not tradable (tradable=%s, status=%s).",
                symbol, tradable, status,
            )
            return False
        return True
    except Exception as exc:
        log.warning("[%s] Could not verify asset tradability: %s", symbol, exc)
        return False


def count_open_positions(trading_client) -> int:
    """Return the total number of currently open positions across the account."""
    try:
        return len(trading_client.get_all_positions())
    except Exception as exc:
        log.warning("Could not count open positions: %s", exc)
        return 0


def get_open_position_symbols(trading_client) -> Set[str]:
    """
    Return the set of symbol names with currently open positions.

    Used for per-symbol position checks without calling has_open_position
    once per symbol in a loop.
    Returns an empty set on any API failure so callers degrade gracefully.
    """
    try:
        positions = trading_client.get_all_positions()
        return {
            getattr(p, "symbol", "").upper()
            for p in positions
            if getattr(p, "symbol", "")
        }
    except Exception as exc:
        log.warning("Could not fetch open position symbols: %s", exc)
        return set()
