#!/bin/zsh

cd /Users/ai-agent/trading-bot

source bot/venv/bin/activate

mkdir -p bot/logs

# Start API if not already running
if ! lsof -i :8000 >/dev/null 2>&1; then
  nohup uvicorn main:app --host 127.0.0.1 --port 8000 --app-dir apps/api >> bot/logs/api.log 2>&1 &
  sleep 3
fi

# Start AlphaBot if not already running
if ! pgrep -f "apps/api/run_bot.py" >/dev/null 2>&1; then
  nohup python apps/api/run_bot.py >> bot/logs/bot.log 2>&1 &
fi

echo "AlphaBot start command completed."
