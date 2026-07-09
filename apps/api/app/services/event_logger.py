"""
Event logger — writes structured trading events to data/trading_events.jsonl.

Each event line is JSON with:
  timestamp_utc, event_type, symbol, severity, message, data

An in-memory ring buffer (last 500 events) serves the /events endpoint
without touching the file on every read.

Thread-safe: all writes are protected by a threading.Lock.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Log file: apps/api/data/trading_events.jsonl
# __file__ is apps/api/app/services/event_logger.py, so go up 3 levels.
_API_ROOT = Path(__file__).parent.parent.parent
_LOG_DIR  = _API_ROOT / "data"
_LOG_FILE = _LOG_DIR / "trading_events.jsonl"

_lock = threading.Lock()

# Bounded in-memory ring buffer — keeps the /events endpoint fast
_MAX_IN_MEMORY = 500
_recent_events: list = []


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_event(
    event_type: str,
    message: str,
    severity: str = "info",
    symbol: Optional[str] = None,
    data: Optional[dict] = None,
) -> dict:
    """
    Write one event to the JSONL file and the in-memory buffer.
    Returns the event dict. Never raises — silently prints on error.

    severity values: "info" | "warning" | "error" | "success"
    """
    event: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "event_type":    event_type,
        "symbol":        symbol,
        "severity":      severity,
        "message":       message,
        "data":          data or {},
    }
    try:
        _ensure_log_dir()
        with _lock:
            with open(_LOG_FILE, "a") as fh:
                fh.write(json.dumps(event) + "\n")
            _recent_events.append(event)
            # Trim buffer to keep only the latest N events
            if len(_recent_events) > _MAX_IN_MEMORY:
                _recent_events.pop(0)
    except Exception as exc:
        print(f"[event_logger] WARNING: could not write event '{event_type}': {exc}")
    return event


def get_recent_events(limit: int = 50) -> list:
    """Return the most recent events from the in-memory buffer, newest first."""
    with _lock:
        tail = _recent_events[-limit:]
    return list(reversed(tail))


def load_events_from_file(limit: int = 200) -> list:
    """
    Read the last `limit` events from the JSONL file, newest first.
    Falls back to the in-memory buffer if the file is unavailable.
    """
    try:
        if not _LOG_FILE.exists():
            return get_recent_events(limit)
        with _lock:
            raw = _LOG_FILE.read_text()
        lines = [ln for ln in raw.strip().splitlines() if ln.strip()]
        parsed = []
        for line in lines[-limit:]:
            try:
                parsed.append(json.loads(line))
            except Exception:
                pass
        return list(reversed(parsed))
    except Exception:
        return get_recent_events(limit)
