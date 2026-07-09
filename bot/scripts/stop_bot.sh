#!/bin/zsh

echo "Stopping AlphaBot..."

pkill -f "apps/api/run_bot.py" 2>/dev/null || true
pkill -f "uvicorn main:app --host 127.0.0.1 --port 8000" 2>/dev/null || true

rm -f /Users/ai-agent/trading-bot/apps/api/bot.pid
rm -f /Users/ai-agent/trading-bot/bot/bot.pid

echo "AlphaBot stop command completed."
