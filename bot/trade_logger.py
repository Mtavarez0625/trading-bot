from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from strategy import SignalResult

log = logging.getLogger(__name__)

_CSV_PATH = Path(__file__).parent / "trades.csv"

# Column names in the desired format.  Old files used different names for some
# columns (action, quantity, signal_reason) — _migrate_header() renames them
# transparently so existing data is never discarded.
_FIELDNAMES = [
    "timestamp",
    "symbol",
    "side",             # "BUY" for entries, "EXIT_MONITOR" for position snapshots
    "qty",
    "entry_price",
    "stop_loss",
    "take_profit",
    "signal_reasons",
    "ema_20",
    "ema_50",
    "rsi_14",
    "volume",
    "volume_avg",
    "order_id",
    "status",
    "current_price",      # exit monitoring — empty on entry rows
    "unrealized_pl_pct",  # exit monitoring — empty on entry rows
    "exit_action",        # e.g. "break-even" or "trailing-stop"
    "exit_reason",        # human-readable trigger description
    "dry_run",
]

# Maps old column names → new column names used by this version of the logger.
_RENAMES = {
    "action": "side",
    "quantity": "qty",
    "signal_reason": "signal_reasons",
}


def _migrate_header() -> None:
    """
    If trades.csv exists with an old header, rename columns in-place and append
    any new columns so existing data rows are never lost or corrupted.

    Old rows end up with empty values for newly-added columns (order_id, status),
    which is correct — those trades were submitted before order tracking was added.
    """
    if not _CSV_PATH.exists():
        return

    text = _CSV_PATH.read_text()
    lines = text.splitlines(keepends=True)
    if not lines:
        return

    raw_header = lines[0].rstrip("\r\n")
    existing_cols = [c.strip() for c in raw_header.split(",")]

    # Apply renames
    updated_cols = [_RENAMES.get(c, c) for c in existing_cols]

    # Append any columns present in _FIELDNAMES but not yet in the file
    for col in _FIELDNAMES:
        if col not in updated_cols:
            updated_cols.append(col)

    new_header_line = ",".join(updated_cols) + ("\r\n" if lines[0].endswith("\r\n") else "\n")

    if new_header_line == lines[0]:
        return  # Nothing changed — already up to date

    lines[0] = new_header_line
    _CSV_PATH.write_text("".join(lines))
    log.info("trade_logger: CSV header migrated to current schema.")


def _ensure_header() -> None:
    """Create the CSV with the current header, or migrate an existing one."""
    if not _CSV_PATH.exists():
        with _CSV_PATH.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writeheader()
        return
    _migrate_header()


def log_trade(
    symbol: str,
    quantity: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    result: SignalResult,
    dry_run: bool,
    order_id: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    """Append one trade row to trades.csv.  Never raises."""
    try:
        _ensure_header()
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": "BUY",
            "qty": quantity,
            "entry_price": round(entry_price, 4),
            "stop_loss": round(stop_loss, 4),
            "take_profit": round(take_profit, 4),
            "signal_reasons": result.reason,
            "ema_20": _fmt(result.ema_20),
            "ema_50": _fmt(result.ema_50),
            "rsi_14": _fmt(result.rsi_14),
            "volume": _fmt(result.volume),
            "volume_avg": _fmt(result.vol_avg_20),
            "order_id": order_id or "",
            "status": status or "",
            "dry_run": dry_run,
        }
        with _CSV_PATH.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writerow(row)
    except Exception as exc:
        log.error("trade_logger: failed to write trade row: %s", exc)


def log_exit_monitor(
    symbol: str,
    qty: int,
    entry_price: float,
    current_price: float,
    unrealized_pl_pct: float,
    exit_action: str,
    exit_reason: str,
) -> None:
    """Append one EXIT_MONITOR row when a break-even or trailing-stop threshold fires."""
    try:
        _ensure_header()
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": "EXIT_MONITOR",
            "qty": qty,
            "entry_price": round(entry_price, 4),
            "stop_loss": "",
            "take_profit": "",
            "signal_reasons": "",
            "ema_20": "",
            "ema_50": "",
            "rsi_14": "",
            "volume": "",
            "volume_avg": "",
            "order_id": "",
            "status": "",
            "current_price": round(current_price, 4),
            "unrealized_pl_pct": f"{unrealized_pl_pct:.4f}",
            "exit_action": exit_action,
            "exit_reason": exit_reason,
            "dry_run": "",
        }
        with _CSV_PATH.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writerow(row)
    except Exception as exc:
        log.error("trade_logger: failed to write exit monitor row: %s", exc)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"
