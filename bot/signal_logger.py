from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from strategy import SignalResult

log = logging.getLogger(__name__)

_CSV_PATH = Path(__file__).parent / "signals.csv"
_FIELDNAMES = [
    "timestamp",
    "symbol",
    "signal",
    "reason",
    "trend_ok",
    "rsi_ok",
    "volume_ok",
    "vwap_ok",
    "rel_vol_ok",
    "not_extended",
    "spread_ok",
    "ema_20",
    "ema_50",
    "rsi_14",
    "volume",
    "volume_avg",
    "vwap",
    "relative_volume",
]


def _ensure_header() -> None:
    """
    Create signals.csv with the current header, or back up and recreate it
    if the schema has changed (new columns were added to _FIELDNAMES).
    """
    expected_header = ",".join(_FIELDNAMES)

    if _CSV_PATH.exists():
        with _CSV_PATH.open() as f:
            first_line = f.readline().strip()
        if first_line == expected_header:
            return
        # Schema changed — preserve old file and start fresh
        backup = _CSV_PATH.with_suffix(".csv.bak")
        _CSV_PATH.rename(backup)
        log.info(
            "signal_logger: schema updated, old signals.csv backed up to %s", backup
        )

    with _CSV_PATH.open("w", newline="") as f:
        csv.DictWriter(f, fieldnames=_FIELDNAMES).writeheader()


def log_signal(symbol: str, result: SignalResult) -> None:
    """Append one signal evaluation row to signals.csv. Never raises."""
    try:
        _ensure_header()
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "signal": result.signal,
            "reason": result.reason,
            "trend_ok": result.trend_ok,
            "rsi_ok": result.rsi_ok,
            "volume_ok": result.volume_ok,
            "vwap_ok": result.vwap_ok,
            "rel_vol_ok": result.rel_vol_ok,
            "not_extended": result.not_extended,
            "spread_ok": result.spread_ok,
            "ema_20": _fmt(result.ema_20),
            "ema_50": _fmt(result.ema_50),
            "rsi_14": _fmt(result.rsi_14),
            "volume": _fmt(result.volume),
            "volume_avg": _fmt(result.vol_avg_20),
            "vwap": _fmt(result.vwap),
            "relative_volume": _fmt(result.relative_volume),
        }
        with _CSV_PATH.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writerow(row)
    except Exception as exc:
        log.error("signal_logger: failed to write signal row: %s", exc)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"
