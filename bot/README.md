# `bot/` — legacy, non-production

This directory is **not** part of the active trading path. It predates the
current `apps/api/` FastAPI service and is kept in the repository for
historical/reference purposes only.

## What is actually running

- `apps/api/main.py` is the production strategy/execution engine, run under
  `launchd` (`com.tradingbot.api`) via `uvicorn`.
- `apps/api/run_bot.py` is started by `cron` on weekday mornings and drives
  the FastAPI service through its HTTP endpoints.
- `apps/api/journal.py` / `apps/api/trading_journal.db` is the authoritative
  paper-trading journal.

See `apps/api/OPS.md` for the full runtime/deployment picture.

## Rules for this directory

- `bot/trades.csv` is **not** the authoritative trading journal. It is a
  legacy artifact and may still be written to by old code paths or manual
  testing; do not treat it as a source of truth for performance analysis.
- Do not make production strategy changes here. If `bot/` strategy logic is
  ever needed again, that requires first changing the runtime architecture
  (i.e. making this directory part of the active execution path again) —
  not quietly editing it while `apps/api/` remains the thing actually
  placing orders.
- `bot/tests/` may still run and pass; that does not mean this code is live.
