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
    "ema_20",
    "ema_50",
    "rsi_14",
    "volume",
    "volume_avg",
]


def _ensure_header() -> None:
    if not _CSV_PATH.exists():
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
            "ema_20": _fmt(result.ema_20),
            "ema_50": _fmt(result.ema_50),
            "rsi_14": _fmt(result.rsi_14),
            "volume": _fmt(result.volume),
            "volume_avg": _fmt(result.vol_avg_20),
        }
        with _CSV_PATH.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=_FIELDNAMES).writerow(row)
    except Exception as exc:
        log.error("signal_logger: failed to write signal row: %s", exc)


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"
