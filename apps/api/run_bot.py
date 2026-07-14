import os
import signal
import sys
import time
import atexit
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests

BASE_URL = "http://127.0.0.1:8000"
TRADE_URL = f"{BASE_URL}/trade-watchlist"
FLATTEN_URL = f"{BASE_URL}/flatten"
MARKET_STATUS_URL = f"{BASE_URL}/market-status"

# Tracks whether we've already triggered end-of-window flatten today.
_flatten_triggered = False

SLEEP_OPENING_MOMENTUM = 60    # 1 minute — fast cadence 09:35–10:00 ET opening window
SLEEP_MARKET_OPEN      = 300   # 5 minutes — normal cadence after 10:00 ET
SLEEP_MARKET_CLOSED    = 1800  # 30 minutes — used only when market is *confirmed* closed or outside window
SLEEP_RETRY_ERROR      = 60    # 1 minute — used after a transient API/network failure; retries soon
TIMEOUT_SECONDS     = 60

EASTERN = ZoneInfo("America/New_York")

def _parse_window_time(env_var: str, default: dtime) -> dtime:
    raw = os.getenv(env_var, "").strip()
    if raw:
        try:
            parts = raw.split(":")
            return dtime(int(parts[0]), int(parts[1]))
        except Exception:
            pass
    return default

WINDOW_START          = _parse_window_time("TRADING_WINDOW_START", dtime(9, 35))
WINDOW_END            = _parse_window_time("TRADING_WINDOW_END",   dtime(11, 30))
OPENING_MOMENTUM_END  = dtime(10, 0)   # fast-cadence cutover: 09:35–10:00 ET

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


def run_flatten():
    """Call the /flatten endpoint to close all open positions at window end."""
    global _flatten_triggered
    try:
        resp = requests.post(FLATTEN_URL, timeout=TIMEOUT_SECONDS)
        data = resp.json()
        count = data.get("flattened_count", 0)
        if count > 0:
            log_line(f"FLATTEN: closed {count} position(s) at end of trading window")
            for pos in data.get("positions", []):
                log_line(
                    f"  FLATTEN [{pos.get('symbol')}] qty={pos.get('qty')} "
                    f"est_pnl=${pos.get('est_pnl')} | reason={pos.get('reason')}"
                )
        else:
            log_line(f"FLATTEN: no open positions — already flat")
        _flatten_triggered = True
    except Exception as e:
        log_line(f"WARNING: flatten request failed: {e}")


def run_trade_cycle():
    try:
        response = requests.post(TRADE_URL, timeout=TIMEOUT_SECONDS)
        try:
            data = response.json()
            results = data.get("results", [])

            # ── Position status lines (from monitor, logged before trade decisions) ──
            for ps in data.get("position_statuses", []):
                sym    = ps.get("symbol", "?")
                status = ps.get("status", "?")
                if status == "OPEN":
                    log_line(
                        f"  POS    [{sym}] entry=${ps.get('entry_price')} "
                        f"cur=${ps.get('current_price')} "
                        f"P&L=${ps.get('unrealized_pnl')} ({ps.get('unrealized_pct')}%) "
                        f"stop={ps.get('stop_price') or 'n/a'} "
                        f"TP={ps.get('take_profit_price') or 'n/a'}"
                    )
                elif status == "BOT_EXITED":
                    log_line(
                        f"  BOT_EXIT [{sym}] reason={ps.get('exit_status')} "
                        f"exit=${ps.get('current_price')} entry=${ps.get('entry_price')} "
                        f"pnl=${ps.get('unrealized_pnl')} "
                        f"source={ps.get('exit_trigger_source', 'bot_hard_exit')}"
                    )
                elif status == "AUTO_CLOSED":
                    log_line(
                        f"  AUTO_EXIT [{sym}] reason={ps.get('exit_reason')} "
                        f"exit=${ps.get('exit_price')} entry=${ps.get('entry_price')} "
                        f"est_pnl=${ps.get('est_pnl')} "
                        f"source={ps.get('exit_trigger_source', 'alpaca_bracket')}"
                    )

            # Tally cycle stats
            scanned       = len(results)
            signals       = sum(1 for r in results if r.get("signal") == "BUY")
            entered       = sum(1 for r in results if r.get("new_entry_opened"))
            errors        = sum(1 for r in results if r.get("signal") == "ERROR")
            # Count blocked: any result with blocked_by set (includes HOLD-filtered signals)
            blocked_total = sum(1 for r in results if r.get("blocked_by"))
            near_misses   = sum(1 for r in results if r.get("near_miss"))
            exited        = sum(1 for r in results if r.get("signal") == "SELL" and r.get("starting_qty", 0) > 0)

            # Build per-blocker breakdown string
            blocker_counts: dict = {}
            for r in results:
                b = r.get("blocked_by")
                if b:
                    blocker_counts[b] = blocker_counts.get(b, 0) + 1
            blocker_detail = ""
            if blocker_counts:
                parts = [f"{k}×{v}" for k, v in sorted(blocker_counts.items())]
                blocker_detail = f" [{', '.join(parts)}]"

            near_miss_tag = f" | {near_misses} near-miss" if near_misses else ""
            log_line(
                f"Cycle: {scanned} scanned | {signals} BUY | {entered} entered | "
                f"{blocked_total} blocked{blocker_detail} | {exited} exited | "
                f"{errors} errors{near_miss_tag}"
            )

            # Per-symbol lines
            for item in results:
                sym     = item.get("symbol", "?")
                sig     = item.get("signal", "?")
                summary = item.get("decision_summary") or item.get("signal_reason") or item.get("message", "")
                blocked = item.get("blocked_by", "")
                qty     = item.get("starting_qty", 0)
                tier    = item.get("entry_tier", "")
                score   = item.get("score")
                grade   = item.get("grade", "")
                tier_tag   = f" [{tier}]" if tier else ""
                score_tag  = f" score={score}[{grade}]" if score is not None else ""
                if item.get("error"):
                    log_line(f"  ERROR  [{sym}] {item.get('error')}")
                elif sig == "ERROR":
                    log_line(f"  ERROR  [{sym}] {summary}")
                elif item.get("new_entry_opened"):
                    log_line(f"  ENTER  [{sym}]{tier_tag}{score_tag} | {summary}")
                elif sig == "SELL" and qty > 0:
                    log_line(f"  EXIT   [{sym}] {summary}")
                elif item.get("near_miss"):
                    gaps = item.get("near_miss_gaps", "")
                    log_line(f"  NEAR_MISS [{sym}]{score_tag} | missing: {gaps} | {summary}")
                elif blocked:
                    log_line(f"  BLOCK  [{sym}]{score_tag} reason={blocked} | {summary}")
                elif sig == "BUY":
                    log_line(f"  BUY    [{sym}]{tier_tag}{score_tag} | {summary}")
                else:
                    log_line(f"  {sig:<5}  [{sym}]{tier_tag}{score_tag} | {summary}")
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


def _handle_sigterm(signum, frame):
    # Exit via sys.exit so atexit runs and the PID lock file is removed.
    log_line("Received SIGTERM — shutting down cleanly")
    sys.exit(0)


if __name__ == "__main__":
    _acquire_instance_lock()
    signal.signal(signal.SIGTERM, _handle_sigterm)
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
                # Reset the daily flatten flag when the market is closed overnight.
                # This ensures flatten fires again the next trading day.
                _flatten_triggered = False

                # Between 9:00 and window start (9:35) poll every 60s to catch the open promptly.
                et = get_eastern_time()
                if dtime(9, 0) <= et < WINDOW_START:
                    log_line(
                        f"SLEEP REASON: Pre-market approach ({et.strftime('%H:%M')} ET, "
                        f"window starts {WINDOW_START.strftime('%H:%M')}) — sleeping 60s"
                    )
                    sleep_seconds = 60
                else:
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
                    # ── Flatten at window end (once per day) ─────────────────
                    if not _flatten_triggered:
                        log_line(
                            f"TRADING WINDOW ENDED ({et.strftime('%H:%M')} ET) — "
                            f"triggering end-of-window flatten"
                        )
                        run_flatten()
                    else:
                        log_line(
                            f"SLEEP REASON: After trading window ({et.strftime('%H:%M')} ET, "
                            f"window ended {WINDOW_END.strftime('%H:%M')}) — sleeping 30 min"
                        )
                    sleep_seconds = SLEEP_MARKET_CLOSED
                else:
                    run_trade_cycle()
                    if et < OPENING_MOMENTUM_END:
                        log_line(
                            f"SLEEP REASON: Opening momentum cadence (60s) "
                            f"({et.strftime('%H:%M')} ET)"
                        )
                        sleep_seconds = SLEEP_OPENING_MOMENTUM
                    else:
                        log_line(
                            f"SLEEP REASON: Normal cadence after 10:00 ET (300s) "
                            f"({et.strftime('%H:%M')} ET)"
                        )
                        sleep_seconds = SLEEP_MARKET_OPEN

            log_line(f"Sleeping {sleep_seconds}s ...")
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        log_line("Bot runner stopped by user (KeyboardInterrupt)")
