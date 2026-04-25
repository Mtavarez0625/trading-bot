from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from strategy import SignalResult

log = logging.getLogger(__name__)

_CSV_PATH = Path(__file__).parent / "trades.csv"
_FIELDNAMES = [
    "timestamp",
    "symbol",
    "action",
    "quantity",
    "entry_price",
    "stop_loss",
    "take_profit",
    "signal_reason",
    "ema_20",
    "ema_50",
    "rsi_14",
    "volume",
    "volume_avg",
    "dry_run",
]


def _ensure_header() -> None:
    if not _CSV_PATH.exists():
        with _CSV_PATH.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writeheader()


def log_trade(
    symbol: str,
    quantity: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    result: SignalResult,
    dry_run: bool,
) -> None:
    """Append one trade row to trades.csv. Never raises."""
    try:
        _ensure_header()
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "action": "BUY",
            "quantity": quantity,
            "entry_price": round(entry_price, 4),
            "stop_loss": round(stop_loss, 4),
            "take_profit": round(take_profit, 4),
            "signal_reason": result.reason,
            "ema_20": _fmt(result.ema_20),
            "ema_50": _fmt(result.ema_50),
            "rsi_14": _fmt(result.rsi_14),
            "volume": _fmt(result.volume),
            "volume_avg": _fmt(result.vol_avg_20),
            "dry_run": dry_run,
        }
        with _CSV_PATH.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writerow(row)
    except Exception as exc:
        log.error("trade_logger: failed to write trade row: %s", exc)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"
