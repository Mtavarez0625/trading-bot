from __future__ import annotations

import math

# Hard cap: a single position can never exceed this fraction of equity.
# Matches the default max_allocation_pct used in compute_quantity().
MAX_TRADE_EQUITY_FRACTION: float = 0.20


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
    max_allocation_pct: float = MAX_TRADE_EQUITY_FRACTION,
) -> int:
    """
    Calculate share quantity using fixed-fractional risk sizing.

    Formula:
        risk_amount    = equity * risk_per_trade
        stop_distance  = entry_price * stop_loss_pct
        raw_quantity   = risk_amount / stop_distance

    The result is floored and capped so one position never exceeds
    max_allocation_pct of equity.
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

    # Hard cap: a single position cannot consume more than max_allocation_pct of equity
    max_affordable = int(math.floor((equity * max_allocation_pct) / entry_price))
    qty = min(qty, max_affordable)

    return max(qty, 0)
