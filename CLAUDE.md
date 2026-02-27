# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Telegram bot that acts as a remote interface to the Claude Code CLI. Users send messages via Telegram, the bot forwards them to `claude` CLI via `--output-format stream-json`, streams real-time activity updates back, and returns the final response.

## Prerequisites

- Python 3.10+ (uses PEP 585 type hints like `dict[int, str]`)
- `claude` CLI installed and authenticated on PATH
- A Telegram bot token from @BotFather

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run directly (reads .env)
python3 telegram_bot.py

# Run a named instance (reads instances/<name>.env)
python3 telegram_bot.py --instance main

# Background management
./start.sh <instance_name|all>    # start instance(s), logs to logs/<name>.log
./stop.sh <instance_name|all>     # stop instance(s) via SIGTERM then SIGKILL
```

No test suite or linter is configured.

## Architecture

**Single-file design**: All logic lives in `telegram_bot.py` (~1000 lines), organized into sections with ASCII-art dividers:

1. **Configuration** — Env vars loaded via `python-dotenv` from `.env` or `instances/<name>.env`
2. **Session & Lock management** — In-memory dicts track sessions, locks, processes, and queue depth per user
3. **Stream-JSON parsing** — Processes Claude CLI's `stream-json` output line-by-line; extracts activity descriptions and final results
4. **`call_claude()`** — Core function: spawns `claude` as async subprocess, manages heartbeat updates, stall warnings, and result extraction
5. **Message splitting** — Splits long responses at newline/space boundaries for Telegram's 4096-char limit (uses 4000 safety margin)
6. **Telegram handlers** — Command handlers (`/start`, `/clear`, `/new`, `/switch`, `/sessions`, `/status`, `/cd`, `/session`, `/kill`) and message/file handlers
7. **Main** — Builds `Application` with `concurrent_updates=True` and runs polling

## Key Concurrency Model

- Each Claude session has its own `asyncio.Lock` — different sessions run in parallel, same-session messages are serialized and queued
- `session_pending` tracks queue depth per session for user visibility
- `session_processes` stores running subprocess references, enabling `/kill`

## Session Lifecycle

1. First call to a session: uses `--session-id <uuid4>` to create a new Claude conversation
2. Subsequent calls: uses `--resume <uuid4>` to continue the conversation
3. `session_initialized` dict tracks which sessions have had their first call
4. If `--session-id` collides (unlikely), auto-retries with `--resume`

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USERS` | (empty = auto-register first user) | Comma-separated Telegram user IDs |
| `CLAUDE_WORK_DIR` | cwd | Working directory for Claude CLI |
| `CLAUDE_TIMEOUT` | 0 (disabled) | Max seconds per Claude call |
| `HEARTBEAT_INTERVAL` | 30 | Seconds between "Thinking..." updates |
| `STALL_WARN_TIMEOUT` | 300 | Seconds before stall warning |
| `CLAUDE_MAX_TURNS` | 30 | Max agentic turns per call |
| `UPLOAD_DIR` | `$CLAUDE_WORK_DIR/uploads` | Where uploaded files are saved |

## Important Implementation Details

- `CLAUDECODE` env var is explicitly removed from the subprocess environment before spawning Claude CLI (prevents nested detection)
- Claude is invoked with `--dangerously-skip-permissions` flag
- Stream-json result extraction has a priority chain: result event > error subtype > last text-only assistant turn > leftover text > stderr fallback
- Text from assistant events preceding a `tool_use` is discarded as intermediate narration
- Auth: if `TELEGRAM_ALLOWED_USERS` is empty, the first `/start` user is auto-registered

## Multi-Instance Support

Named instances store their config in `instances/<name>.env`. The `start.sh`/`stop.sh` scripts manage PID files in `logs/<name>.pid` and output logs in `logs/<name>.log`.
