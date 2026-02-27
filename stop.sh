#!/bin/bash
# Stop telegram bot instance(s)
# Usage: ./stop.sh main | rena | all

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGS_DIR="$SCRIPT_DIR/logs"

stop_instance() {
    local name="$1"
    local pid_file="$LOGS_DIR/${name}.pid"

    if [ ! -f "$pid_file" ]; then
        echo "[$name] Not running (no PID file)"
        return 0
    fi

    local pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid"
        fi
        echo "[$name] Stopped (PID $pid)"
    else
        echo "[$name] Not running (stale PID $pid)"
    fi
    rm -f "$pid_file"
}

case "${1:-}" in
    all)
        for env_file in "$SCRIPT_DIR"/instances/*.env; do
            name=$(basename "$env_file" .env)
            stop_instance "$name"
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
        stop_instance "$1"
        ;;
esac
