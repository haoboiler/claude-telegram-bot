#!/bin/bash
# Start telegram bot instance(s)
# Usage: ./start.sh main | rena | all

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGS_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOGS_DIR"

start_instance() {
    local name="$1"
    local env_file="$SCRIPT_DIR/instances/${name}.env"

    if [ ! -f "$env_file" ]; then
        echo "Error: $env_file not found"
        return 1
    fi

    # Check if already running
    local pid_file="$LOGS_DIR/${name}.pid"
    if [ -f "$pid_file" ]; then
        local old_pid=$(cat "$pid_file")
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "[$name] Already running (PID $old_pid)"
            return 0
        fi
    fi

    # Start bot (use py11 conda env which has all dependencies)
    local PYTHON="/home/b0qi/anaconda3/envs/py11/bin/python"
    nohup "$PYTHON" "$SCRIPT_DIR/telegram_bot.py" --instance "$name" \
        > "$LOGS_DIR/${name}.log" 2>&1 &
    local pid=$!
    echo "$pid" > "$pid_file"
    echo "[$name] Started (PID $pid), log: $LOGS_DIR/${name}.log"
}

case "${1:-}" in
    all)
        for env_file in "$SCRIPT_DIR"/instances/*.env; do
            name=$(basename "$env_file" .env)
            start_instance "$name"
        done
        ;;
    "")
        echo "Usage: $0 <instance_name|all>"
        echo "Available instances:"
        for env_file in "$SCRIPT_DIR"/instances/*.env; do
            echo "  - $(basename "$env_file" .env)"
        done
        ;;
    *)
        start_instance "$1"
        ;;
esac
