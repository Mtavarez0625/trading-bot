#!/bin/bash
# Start the bot runner from the correct working directory.
# run_bot.py resolves bot.log and bot.pid relative to this directory,
# and the venv python must be used. Called by cron at 09:25 ET Mon-Fri.
cd "$(dirname "$0")" || exit 1
exec ./venv/bin/python3 run_bot.py
