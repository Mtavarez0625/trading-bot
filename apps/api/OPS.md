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

## Execution modes

`main._execution_mode_fields()` is the single source of truth for what the bot is
actually doing with orders — reported on `/health`, `/dashboard-data`,
`/strategy-status`, startup logs, event logs, and the Telegram startup message.

| `execution_mode`   | Meaning |
|---------------------|---------|
| `dry_run`           | `DRY_RUN=true`. No orders are submitted anywhere — everything is simulated in-process. |
| `paper_live`        | `DRY_RUN=false`, `ALPACA_PAPER=true`, `ALLOW_LIVE_TRADING=false`. **The normal state.** Real orders are submitted, but only to Alpaca's paper endpoint — no real money moves. |
| `live_money`        | `ALPACA_PAPER=false` **and** `ALLOW_LIVE_TRADING=true`. Real money at risk. Not currently enabled anywhere in this deployment. |
| `live_locked_out`   | `ALPACA_PAPER=false` but `ALLOW_LIVE_TRADING=false` — a startup hard-lock refuses to run this combination; labeled explicitly as a defensive measure. |

`dry_run` (simulation, nothing sent to a broker) is not the same thing as
`paper_live` (orders sent to Alpaca's paper account, no real money) is not the same
thing as `live_money` (orders sent to Alpaca's real-money account). Legacy fields
`bot_mode`, `dry_run`, `paper_mode` are kept on API responses for backward
compatibility but are deprecated in favor of `execution_mode` / `paper_trading` /
`real_money_trading` — see `apps/web/lib/types.ts`.

**Strategy settings (thresholds, watchlist, sizing, stops, entry windows, regime
rules) were not changed as part of this work — only how execution state is
reported and how the journal/analytics handle broker fills.**

## Journal reconciliation

At startup, `_reconcile_journal_state()` compares journal-open positions against
real Alpaca positions. For any journal entry Alpaca no longer backs, it now
searches Alpaca's closed orders (`_find_broker_closing_fill`, shared with
cycle-time bracket reconciliation) for the real closing fill before touching the
journal:
- fill found → closed with the confirmed price, `exit_reason="reconciled_<type>"`,
  `data_quality_status="verified"`.
- no fill found → closed with `exit_price=NULL`, `exit_reason="unresolved_reconciliation"`,
  `data_quality_status="unresolved_reconciliation"`. **No price is ever invented.**
- `DRY_RUN` journal entries never had a real Alpaca order behind them, so they're
  cleared with `exit_reason="reconcile_stale"`, tagged `data_quality_status="suspect_zero_exit"`.

## Data-quality exclusions

`paper_trades.data_quality_status` (`verified` / `suspect_zero_exit` /
`unresolved_reconciliation` / `pending_entry_fill`) marks whether a row is
trustworthy. `journal.ELIGIBLE_TRADE_SQL` is the single shared predicate every
performance endpoint filters through (win rate, profit factor, expectancy,
drawdown, equity curve, Hermes summaries, etc.) — non-`verified` rows, zero/NULL
exit or entry prices, and known-bad exit reasons are excluded automatically.
Raw journal/history endpoints (`/recent-trades`, daily exit listings) are **not**
filtered — they show every row, including suspect ones, with
`data_quality_status` attached for transparency. Performance endpoints report
`eligible_trade_count` / `excluded_trade_count` / `excluded_trade_ids` /
`data_quality_warning` alongside their normal metrics.

Historical rows `paper_trades` ids 4 and 5 are known `exit_price=0.0` artifacts
from before this fix (`reconcile_stale` / `auto_closed_bracket`). They were **not**
deleted or rewritten; tagging them via `journal.mark_paper_trade_data_quality()`
is a deliberate, approved-before-running administrative step (see PR notes), not
an automatic migration.

## Pre-session verification

```bash
grep -E '^(ALPACA_PAPER|ALLOW_LIVE_TRADING|ALPACA_BASE_URL|DRY_RUN)=' apps/api/.env
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
curl -s http://127.0.0.1:8000/dashboard-data | python3 -m json.tool
```
Expect `ALPACA_PAPER=true`, `ALLOW_LIVE_TRADING=false`,
`ALPACA_BASE_URL=https://paper-api.alpaca.markets`, and `/health` reporting
`execution_mode="paper_live"`, `paper_trading=true`, `real_money_trading=false`,
`live_trading_locked=true`.
