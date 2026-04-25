from __future__ import annotations

import math


# Hard cap: a single trade cannot consume more than this fraction of equity.
MAX_TRADE_EQUITY_FRACTION = 0.20


def compute_stop_price(entry_price: float, stop_loss_pct: float) -> float:
    """Return stop price = entry * (1 - stop_loss_pct), rounded to 2 decimal places."""
    return round(entry_price * (1.0 - stop_loss_pct), 2)


def compute_take_profit_price(entry_price: float, take_profit_pct: float) -> float:
    """Return take-profit price = entry * (1 + take_profit_pct), rounded to 2 decimal places."""
    return round(entry_price * (1.0 + take_profit_pct), 2)


def compute_quantity(
    equity: float,
    risk_per_trade: float,
    entry_price: float,
    stop_loss_pct: float,
) -> int:
    """
    Calculate share quantity using fixed-fractional risk sizing.

    Formula:
        risk_amount    = equity * risk_per_trade
        stop_distance  = entry_price * stop_loss_pct
        raw_quantity   = risk_amount / stop_distance

    The result is then floored and capped at MAX_TRADE_EQUITY_FRACTION of equity.
    Returns 0 if any input is invalid or if calculated quantity rounds down to 0.
    """
    if equity <= 0 or entry_price <= 0 or stop_loss_pct <= 0 or risk_per_trade <= 0:
        return 0

    risk_amount = equity * risk_per_trade
    stop_distance = entry_price * stop_loss_pct

    if stop_distance <= 0:
        return 0

    raw_qty = risk_amount / stop_distance
    qty = int(math.floor(raw_qty))

    if qty < 1:
        return 0

    # Hard cap: never risk more than MAX_TRADE_EQUITY_FRACTION of equity on one trade
    max_affordable = int(math.floor((equity * MAX_TRADE_EQUITY_FRACTION) / entry_price))
    qty = min(qty, max_affordable)

    return max(qty, 0)
