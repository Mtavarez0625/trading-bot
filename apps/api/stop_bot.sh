#!/bin/bash
# Gracefully stop the bot runner started by cron.
# Verifies the PID in bot.pid actually belongs to run_bot.py before killing,
# uses SIGTERM first, and only escalates to SIGKILL if the process won't exit.
API_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$API_DIR/bot.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "[stop_bot] no bot.pid — nothing to stop"
    exit 0
fi

PID="$(cat "$PID_FILE")"
if ! [[ "$PID" =~ ^[0-9]+$ ]]; then
    echo "[stop_bot] bot.pid is unreadable — removing stale file"
    rm -f "$PID_FILE"
    exit 0
fi

# Guard against PID reuse: only kill if the process is actually run_bot.py.
if ! ps -p "$PID" -o command= | grep -q "run_bot.py"; then
    echo "[stop_bot] PID $PID is not run_bot.py (stale or reused) — removing stale file"
    rm -f "$PID_FILE"
    exit 0
fi

echo "[stop_bot] sending SIGTERM to bot runner (PID $PID)"
kill "$PID"

for _ in $(seq 1 15); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "[stop_bot] bot runner exited cleanly"
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 1
done

echo "[stop_bot] bot runner did not exit after 15s — escalating to SIGKILL"
kill -9 "$PID"
rm -f "$PID_FILE"
