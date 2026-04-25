import os
import sys
import time
import atexit
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests

BASE_URL = "http://127.0.0.1:8000"
TRADE_URL = f"{BASE_URL}/trade-watchlist"
MARKET_STATUS_URL = f"{BASE_URL}/market-status"

SLEEP_MARKET_OPEN   = 300   # 5 minutes — normal cadence during the active window
SLEEP_MARKET_CLOSED = 1800  # 30 minutes — used only when market is *confirmed* closed or outside window
SLEEP_RETRY_ERROR   = 60    # 1 minute — used after a transient API/network failure; retries soon
TIMEOUT_SECONDS     = 60

EASTERN = ZoneInfo("America/New_York")
WINDOW_START = dtime(9, 30)
WINDOW_END = dtime(11, 30)

# ── Single-instance lock (PID file) ──────────────────────────────────────────
_LOCK_FILE = os.path.join(os.path.dirname(__file__), "bot.pid")


def _acquire_instance_lock():
    """Exit immediately if another bot runner is already active."""
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                existing_pid = int(f.read().strip())
            # Check if that PID is still alive
            os.kill(existing_pid, 0)
            print(
                f"[lock] ABORT: another bot runner is already active (PID {existing_pid}). "
                f"If that process is gone, delete {_LOCK_FILE} and restart."
            )
            sys.exit(1)
        except (ProcessLookupError, PermissionError):
            # PID file is stale — the old process is gone
            print(f"[lock] Stale PID file found (PID {existing_pid}) — overwriting")
        except (ValueError, OSError):
            print(f"[lock] Unreadable PID file — overwriting")

    with open(_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    atexit.register(_release_instance_lock)
    print(f"[lock] Instance lock acquired (PID {os.getpid()})")


def _release_instance_lock():
    try:
        os.remove(_LOCK_FILE)
    except OSError:
        pass


def get_eastern_time() -> dtime:
    return datetime.now(EASTERN).time()


def log_line(message: str):
    timestamp = datetime.now().isoformat()
    line = f"[{timestamp}] {message}"
    print(line)
    with open("bot.log", "a") as f:
        f.write(line + "\n")


def check_market_open():
    """
    Returns:
      True  — market is confirmed open by the API
      False — market is confirmed closed by the API
      None  — transient failure (timeout / network error); caller should retry soon,
              NOT treat this as confirmed-closed and sleep 30 minutes.
    """
    try:
        response = requests.get(MARKET_STATUS_URL, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return bool(response.json().get("is_open", False))
    except requests.exceptions.Timeout:
        log_line(
            "WARNING: market-status request timed out — status unknown, "
            f"will retry in {SLEEP_RETRY_ERROR}s (not treating as closed)"
        )
    except requests.exceptions.RequestException as e:
        log_line(
            f"WARNING: could not reach market-status endpoint: {e} — "
            f"will retry in {SLEEP_RETRY_ERROR}s (not treating as closed)"
        )
    except Exception as e:
        log_line(
            f"WARNING: unexpected error checking market status: {e} — "
            f"will retry in {SLEEP_RETRY_ERROR}s (not treating as closed)"
        )
    return None  # signals transient failure — retry soon


def log_config():
    """Fetch and log the active strategy config on startup for visibility."""
    try:
        resp = requests.get(f"{BASE_URL}/config-summary", timeout=10)
        resp.raise_for_status()
        cfg = resp.json()
        log_line(f"Active config: {cfg}")
    except Exception as e:
        log_line(f"WARNING: Could not fetch config summary: {e}")


def run_trade_cycle():
    try:
        response = requests.post(TRADE_URL, timeout=TIMEOUT_SECONDS)
        try:
            data = response.json()
            results = data.get("results", [])

            # Tally cycle stats for the one-line summary
            scanned       = len(results)
            buy_count     = sum(1 for r in results if r.get("signal") == "BUY")
            entered       = sum(1 for r in results if r.get("new_entry_opened"))
            errors        = sum(1 for r in results if r.get("signal") == "ERROR")
            cooldown_skip = sum(1 for r in results if r.get("blocked_by") == "cooldown")
            stale_skip    = sum(1 for r in results if r.get("blocked_by") == "stale_data")
            market_skip   = sum(1 for r in results if "market closed" in (r.get("decision_summary") or "").lower())
            blocked_total = sum(1 for r in results if r.get("blocked_by"))
            sell_count    = sum(1 for r in results if r.get("signal") == "SELL" and r.get("starting_qty", 0) > 0)

            log_line(
                f"Cycle: {scanned} scanned | {buy_count} BUY | {entered} entered | "
                f"{sell_count} exited | {blocked_total} blocked | {errors} errors | "
                f"{cooldown_skip} cooldown | {stale_skip} stale | {market_skip} mkt-closed"
            )

            # Per-symbol line (ERROR paths always printed; others condensed)
            for item in results:
                sym     = item.get("symbol", "?")
                sig     = item.get("signal", "?")
                summary = item.get("decision_summary") or item.get("signal_reason") or item.get("message", "")
                blocked = item.get("blocked_by", "")
                qty     = item.get("starting_qty", 0)
                tier    = item.get("entry_tier", "")
                tier_tag = f" [{tier}]" if tier else ""
                if sig == "ERROR" or item.get("error"):
                    log_line(f"  ERROR [{sym}] {summary}")
                elif blocked:
                    log_line(f"  [{sym}] {sig}{tier_tag} | SKIP({blocked}) | {summary} | qty={qty}")
                else:
                    log_line(f"  [{sym}] {sig}{tier_tag} | {summary} | qty={qty}")
        except Exception:
            log_line(f"Trade cycle response (raw): {response.text}")
        response.raise_for_status()
        return True
    except requests.exceptions.Timeout:
        log_line("Error: trade cycle request timed out")
    except requests.exceptions.HTTPError as e:
        log_line(f"HTTP error during trade cycle: {e}")
    except requests.exceptions.RequestException as e:
        log_line(f"Request error during trade cycle: {e}")
    except Exception as e:
        log_line(f"Unexpected error during trade cycle: {e}")
    return False


if __name__ == "__main__":
    _acquire_instance_lock()
    log_line("Bot runner started")
    log_config()
    try:
        while True:
            market_open = check_market_open()

            if market_open is None:
                # Transient API/network failure — do NOT assume the market is closed.
                # Sleep a short interval and retry to avoid missing the trading window.
                log_line(
                    f"RETRY: Market status unknown due to endpoint error — "
                    f"sleeping {SLEEP_RETRY_ERROR}s before retry"
                )
                sleep_seconds = SLEEP_RETRY_ERROR

            elif not market_open:
                # API confirmed market is closed — safe to sleep the long interval.
                log_line("SLEEP REASON: Market confirmed CLOSED by API — sleeping 30 min")
                sleep_seconds = SLEEP_MARKET_CLOSED

            else:
                # Market is confirmed open — now check the trading window.
                et = get_eastern_time()
                if et < WINDOW_START:
                    log_line(
                        f"SLEEP REASON: Before trading window ({et.strftime('%H:%M')} ET, "
                        f"window starts {WINDOW_START.strftime('%H:%M')}) — sleeping 5 min"
                    )
                    sleep_seconds = SLEEP_MARKET_OPEN
                elif et >= WINDOW_END:
                    log_line(
                        f"SLEEP REASON: After trading window ({et.strftime('%H:%M')} ET, "
                        f"window ended {WINDOW_END.strftime('%H:%M')}) — sleeping 30 min"
                    )
                    sleep_seconds = SLEEP_MARKET_CLOSED
                else:
                    log_line(
                        f"SLEEP REASON: Normal cadence after trade cycle "
                        f"({et.strftime('%H:%M')} ET) — sleeping 5 min"
                    )
                    run_trade_cycle()
                    sleep_seconds = SLEEP_MARKET_OPEN

            log_line(f"Sleeping {sleep_seconds}s ...")
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        log_line("Bot runner stopped by user (KeyboardInterrupt)")
