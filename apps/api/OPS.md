# Operations Runbook (Mac mini)

## Architecture
- **API** — FastAPI/uvicorn on `127.0.0.1:8000`, managed by a **launchd LaunchAgent**
  (`~/Library/LaunchAgents/com.tradingbot.api.plist`). Starts at login, restarts
  automatically if it crashes (`KeepAlive`). Exactly one instance.
- **Bot runner** — `run_bot.py`, started by **cron** at 09:25 ET Mon–Fri via
  `start_bot.sh`, stopped at 16:30 ET via `stop_bot.sh` (SIGTERM first, SIGKILL
  only after 15 s). Single-instance PID lock in `bot.pid`; stale PID files are
  detected and overwritten on startup. No weekend runs (cron `1-5`).
- Trading window 09:35–11:30 ET, last new entry 11:00, flatten at 11:30
  (handled inside the bot/API — not by the scheduler).

## Logs
- `apps/api/bot.log` — bot runner log (written directly by `run_bot.py`).
- `apps/api/bot.cron.log` — cron-captured stdout/stderr (startup errors,
  tracebacks, stop_bot output, lock aborts).
- `apps/api/api.log` — API stdout/stderr (written by launchd).

## Manual commands
```bash
# API (launchd)
launchctl print gui/$(id -u)/com.tradingbot.api          # status
launchctl kickstart -k gui/$(id -u)/com.tradingbot.api   # restart
launchctl bootout gui/$(id -u)/com.tradingbot.api        # stop (until next login)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradingbot.api.plist  # start

# Bot
apps/api/start_bot.sh   # start (refuses if already running)
apps/api/stop_bot.sh    # graceful stop, removes bot.pid
```

## Sleep / reboot requirements
- The Mac mini **must stay awake and powered on** during market hours.
  Current `pmset` config: `sleep 0` on AC power — the machine never sleeps.
  Do not change this without adding a `pmset repeat wake` schedule.
- After a **reboot**, the API starts automatically at login (LaunchAgent);
  auto-login must remain enabled for that to happen without a keyboard.
  Cron entries survive reboot. Nothing needs a manual restart.
- Cron does **not** fire for missed slots: if the Mac is off/asleep at
  09:25 ET, the bot will not start that day. Keep the machine on.

## Safety invariants (do not change casually)
- `ALPACA_PAPER=true`, `ALLOW_LIVE_TRADING=false` in `apps/api/.env`.
- `REQUIRE_FLAT_START=true`, `FLATTEN_AT_WINDOW_END=true`.
