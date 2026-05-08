from __future__ import annotations

"""
Exit manager: break-even protection and trailing-stop logic.

EXECUTION STATUS
----------------
Position monitoring  : EXECUTED each cycle — all open positions are logged.
Break-even protection: LOG-ONLY.
Trailing stop        : LOG-ONLY.

WHY log-only for stop modifications
-------------------------------------
Alpaca bracket orders create two child orders (take-profit limit + stop-loss
stop).  The child order IDs are not captured at entry time in the current bot.
Replacing the stop leg atomically requires the child order ID; without it we
cannot guarantee we are modifying the right order and cannot prevent a race
between the existing leg filling and our replacement request.

To enable execution in a future iteration:
  1. In trader.submit_bracket_order(), fetch the created order's legs and
     return the stop-loss child order ID alongside the parent order.
  2. Persist the mapping symbol → stop_order_id in TradingState.
  3. Pass that mapping into monitor_and_manage_exits() via stop_order_ids.
  At that point attempt_stop_modification() will submit the replacement and
  return True.
"""

from typing import Dict, Optional

from logger import get_logger

log = get_logger(__name__)

# ── Thresholds (as decimal fractions) ────────────────────────────────────────
BREAK_EVEN_TRIGGER_PCT = 0.010    # +1.0 % unrealised gain → move stop to entry
TRAILING_STOP_TRIGGER_PCT = 0.015  # +1.5 % unrealised gain → compute trail
TRAILING_STOP_TRAIL_PCT = 0.0075   # trail 0.75 % below current price


# ── Pure calculation helpers ──────────────────────────────────────────────────

def calc_unrealized_pct(entry_price: float, current_price: float) -> float:
    """Return unrealised P/L as a decimal fraction (positive = gain)."""
    if entry_price <= 0:
        return 0.0
    return (current_price - entry_price) / entry_price


def should_apply_break_even(unrealized_pct: float) -> bool:
    """True when the position has gained enough to move the stop to entry."""
    return unrealized_pct >= BREAK_EVEN_TRIGGER_PCT


def should_apply_trailing_stop(unrealized_pct: float) -> bool:
    """True when the position has gained enough to apply a trailing stop."""
    return unrealized_pct >= TRAILING_STOP_TRIGGER_PCT


def calc_trailing_stop_price(current_price: float) -> float:
    """Return the trailing-stop price: current_price × (1 − TRAILING_STOP_TRAIL_PCT)."""
    return round(current_price * (1 - TRAILING_STOP_TRAIL_PCT), 2)


# ── Stop-modification attempt (log-only until child IDs are tracked) ──────────

def attempt_stop_modification(
    trading_client,
    stop_order_id: Optional[str],
    new_stop_price: float,
    symbol: str,
    reason: str,
) -> bool:
    """
    Attempt to replace an existing stop order with a new stop price.

    Returns True if the modification was submitted, False otherwise.
    Any exception aborts without raising so the calling cycle continues.

    In the current bot, stop_order_id is always None (child order IDs are not
    captured at submission time), so this always returns False and logs intent
    only.
    """
    if stop_order_id is None:
        log.info(
            "[%s] Stop modification intent (%s) | new_stop=%.2f | "
            "LOG-ONLY: no child order ID available.",
            symbol, reason, new_stop_price,
        )
        return False

    try:
        from alpaca.trading.requests import ReplaceOrderRequest
        req = ReplaceOrderRequest(stop_price=round(new_stop_price, 2))
        trading_client.replace_order_by_id(stop_order_id, req)
        log.info(
            "[%s] Stop order replaced (%s) | order_id=%s | new_stop=%.2f",
            symbol, reason, stop_order_id, new_stop_price,
        )
        return True
    except Exception as exc:
        log.warning(
            "[%s] Stop modification FAILED (%s) | order_id=%s | error=%s",
            symbol, reason, stop_order_id, exc,
        )
        return False


# ── Main entry point called each cycle ───────────────────────────────────────

def monitor_and_manage_exits(
    trading_client,
    stop_order_ids: Optional[Dict[str, str]] = None,
) -> None:
    """
    Check all open positions for break-even / trailing-stop thresholds.

    Logs a snapshot of every open position (symbol, qty, entry, current price,
    unrealised P/L %).  When a threshold is met, logs the intended action and
    calls attempt_stop_modification() (which is LOG-ONLY until child order IDs
    are tracked).

    Args:
        trading_client: Alpaca TradingClient instance.
        stop_order_ids: Optional mapping of symbol → stop child-order-ID.
                        Omit or pass None to stay in log-only mode.
    """
    if stop_order_ids is None:
        stop_order_ids = {}

    try:
        positions = trading_client.get_all_positions()
    except Exception as exc:
        log.warning("Exit monitor: could not fetch positions: %s", exc)
        return

    if not positions:
        log.info("Exit monitor: no open positions.")
        return

    log.info("Exit monitor: checking %d open position(s).", len(positions))

    for pos in positions:
        symbol = getattr(pos, "symbol", "?").upper()
        try:
            qty = float(getattr(pos, "qty", 0))
            entry_price = float(getattr(pos, "avg_entry_price", 0))
            current_price = float(getattr(pos, "current_price", 0))
        except (TypeError, ValueError) as exc:
            log.warning(
                "[%s] Exit monitor: could not parse position fields: %s", symbol, exc
            )
            continue

        unrealized_pct = calc_unrealized_pct(entry_price, current_price)
        unrealized_pl_usd = (current_price - entry_price) * qty

        log.info(
            "[%s] Position | qty=%.0f | entry=%.2f | current=%.2f | "
            "P/L=%.2f%% ($%.2f)",
            symbol, qty, entry_price, current_price,
            unrealized_pct * 100, unrealized_pl_usd,
        )

        stop_order_id = stop_order_ids.get(symbol)

        if should_apply_trailing_stop(unrealized_pct):
            trail_price = calc_trailing_stop_price(current_price)
            log.info(
                "[%s] Trailing-stop logic applies (P/L=%.2f%% >= %.1f%%) | "
                "proposed trail=%.2f",
                symbol, unrealized_pct * 100, TRAILING_STOP_TRIGGER_PCT * 100,
                trail_price,
            )
            attempt_stop_modification(
                trading_client, stop_order_id, trail_price, symbol, "trailing-stop"
            )
            _log_exit_event(
                symbol=symbol,
                qty=qty,
                entry_price=entry_price,
                current_price=current_price,
                unrealized_pct=unrealized_pct,
                exit_action="trailing-stop",
                exit_reason=f"P/L {unrealized_pct*100:.2f}% >= {TRAILING_STOP_TRIGGER_PCT*100:.1f}%",
            )

        elif should_apply_break_even(unrealized_pct):
            log.info(
                "[%s] Break-even protection triggered. (P/L=%.2f%% >= %.1f%%) | "
                "proposed stop=%.2f (entry)",
                symbol, unrealized_pct * 100, BREAK_EVEN_TRIGGER_PCT * 100,
                entry_price,
            )
            attempt_stop_modification(
                trading_client, stop_order_id, entry_price, symbol, "break-even"
            )
            _log_exit_event(
                symbol=symbol,
                qty=qty,
                entry_price=entry_price,
                current_price=current_price,
                unrealized_pct=unrealized_pct,
                exit_action="break-even",
                exit_reason=f"P/L {unrealized_pct*100:.2f}% >= {BREAK_EVEN_TRIGGER_PCT*100:.1f}%",
            )


def _log_exit_event(
    symbol: str,
    qty: float,
    entry_price: float,
    current_price: float,
    unrealized_pct: float,
    exit_action: str,
    exit_reason: str,
) -> None:
    """Write a triggered exit event to the trade log CSV."""
    try:
        from trade_logger import log_exit_monitor
        log_exit_monitor(
            symbol=symbol,
            qty=int(qty),
            entry_price=entry_price,
            current_price=current_price,
            unrealized_pl_pct=unrealized_pct,
            exit_action=exit_action,
            exit_reason=exit_reason,
        )
    except Exception as exc:
        log.warning("[%s] Could not write exit event to CSV: %s", symbol, exc)
