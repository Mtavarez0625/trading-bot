from __future__ import annotations

import uuid
from typing import Optional

from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

from logger import get_logger

log = get_logger(__name__)


def submit_bracket_order(
    trading_client,
    symbol: str,
    qty: int,
    take_profit_price: float,
    stop_loss_price: float,
    dry_run: bool = False,
) -> Optional[object]:
    """
    Submit a market bracket order: BUY entry with a take-profit limit leg
    and a stop-loss leg. Time-in-force is DAY for the entry.

    Args:
        trading_client: Alpaca TradingClient instance.
        symbol:         Ticker symbol, e.g. "AAPL".
        qty:            Integer share quantity (must be >= 1).
        take_profit_price: Limit price for the take-profit leg.
        stop_loss_price:   Stop price for the stop-loss leg.
        dry_run:        If True, log intent but do not submit to Alpaca.

    Returns:
        Order object on success, None on failure or dry run.
    """
    if qty < 1:
        log.error("[%s] Cannot submit order with qty=%d.", symbol, qty)
        return None

    client_order_id = f"bot-{symbol.lower()}-{uuid.uuid4().hex[:12]}"

    log.info(
        "[%s] Order intent | qty=%d | stop=%.2f | tp=%.2f | client_id=%s",
        symbol, qty, stop_loss_price, take_profit_price, client_order_id,
    )

    if dry_run:
        log.info("[%s] DRY RUN — NO ORDER SENT | qty=%d | stop=%.2f | tp=%.2f | client_id=%s",
                 symbol, qty, stop_loss_price, take_profit_price, client_order_id)
        return None

    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
        stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)),
        client_order_id=client_order_id,
    )

    try:
        order = trading_client.submit_order(order_request)
        log.info(
            "[%s] Order submitted | id=%s | status=%s",
            symbol, order.id, order.status,
        )
        return order
    except Exception as exc:
        log.error("[%s] Order submission failed: %s", symbol, exc)
        return None
