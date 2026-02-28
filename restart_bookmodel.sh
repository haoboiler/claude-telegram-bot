#!/bin/bash
# Restart bookmodel instance with new SDK-based bot
# This script waits a few seconds for the current response to be delivered,
# then kills the old process and starts the new one.

set -e

PROJ_DIR="/home/gkh/claude_tasks/claude-telegram-bot"
LOG_DIR="$PROJ_DIR/logs"
PYTHON="/home/b0qi/anaconda3/envs/py11/bin/python"
OLD_PID=$(pgrep -f "telegram_bot.py --instance bookmodel" || true)

echo "[$(date)] Restart script started"

# Wait for current response to be delivered
sleep 5

# Kill old process
if [ -n "$OLD_PID" ]; then
    echo "[$(date)] Killing old bookmodel process (PID: $OLD_PID)"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
    # Force kill if still alive
    kill -9 "$OLD_PID" 2>/dev/null || true
    sleep 1
fi

# Start new process with py11 (has claude-agent-sdk)
echo "[$(date)] Starting new bookmodel with Agent SDK"
cd "$PROJ_DIR"
nohup "$PYTHON" telegram_bot.py --instance bookmodel \
    >> "$LOG_DIR/bookmodel.log" 2>&1 &

NEW_PID=$!
echo "[$(date)] New bookmodel started (PID: $NEW_PID)"
