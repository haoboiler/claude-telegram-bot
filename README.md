# claude-telegram-bot

Telegram Bot that acts as a remote interface to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI, enabling mobile control of Claude Code via Telegram messages.

## Features

- **Multi-Session**: Run multiple independent Claude sessions in parallel (`/new`, `/switch`, `@name msg`)
- **Activity Log**: Cumulative activity log shows every step Claude takes; preserved after completion for review
- **Real-Time Updates**: Throttled live updates (every 3s) show current progress ("Reading file.py", "Running: npm test")
- **Stall Warning**: Warns if no output for 5 minutes (does not auto-kill; use `/kill` instead)
- **File Upload**: Send files from phone to server; optionally forward to Claude with caption
- **Session Routing**: Send messages to specific sessions with `@session_name your message`
- **Message Queue**: Per-session locking with queue position feedback

## Architecture

```
User (Telegram)
  -> Bot (python-telegram-bot, concurrent_updates=True)
    -> resolve session target (@name prefix or active session)
    -> per-session asyncio.Lock (different sessions run in parallel)
    -> claude -p --output-format stream-json --verbose
      -> real-time activity log (cumulative, throttled updates)
      -> heartbeat updates every 30s
      -> stall warning (warn if no events for 5 min, suggest /kill)
    -> Response -> Bot -> User (with [session_name] prefix)
```

## Quick Start

### 1. Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### 2. Install

```bash
git clone https://github.com/haoboiler/claude-telegram-bot.git
cd claude-telegram-bot
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your bot token and settings
```

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | (required) | Bot API token from BotFather |
| `TELEGRAM_ALLOWED_USERS` | (empty) | Comma-separated user IDs (empty = auto-register first user) |
| `CLAUDE_WORK_DIR` | `cwd` | Claude Code working directory |
| `CLAUDE_TIMEOUT` | `0` (disabled) | Total timeout per call (0 = no timeout, use `--max-turns` and `/kill`) |
| `HEARTBEAT_INTERVAL` | `30` | Heartbeat interval (seconds) |
| `STALL_WARN_TIMEOUT` | `300` | Warn (not kill) if no output for N seconds |
| `CLAUDE_MAX_TURNS` | `150` | Max agentic turns per call |
| `UPLOAD_DIR` | `WORK_DIR/uploads` | Directory for files uploaded via Telegram |

### 4. Run

```bash
# Foreground (reads .env)
python3 telegram_bot.py

# Named instance (reads instances/<name>.env)
python3 telegram_bot.py --instance mybot

# Instance management
./start.sh mybot          # start instance, log to logs/mybot.log
./stop.sh mybot           # stop instance
./start.sh all            # start all instances
./stop.sh all             # stop all instances
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Register and see help |
| `/new [name]` | Create new session (old sessions preserved) |
| `/switch <name>` | Switch to existing session |
| `/sessions` | List all sessions with status |
| `/clear` | Reset current session |
| `/status` | Bot status and queue info |
| `/cd <path>` | Change working directory |
| `/session` | Current session info |
| `/kill [name]` | Kill stuck session |
| `@name msg` | Send to specific session without switching |
| (file) | Download to server, reply with saved path |
| (file + caption) | Download + forward path & caption to Claude |

## Multi-Session Usage

```
# Create sessions for different tasks
/new research
/new coding

# Send to specific session without switching
@research what are the latest trends in LLMs?
@coding fix the bug in auth.py

# Different sessions run in parallel!
/sessions  # see status of all sessions
```

## Security Notes

- Uses `--dangerously-skip-permissions` flag for non-interactive mode. **Only use as a personal bot.**
- Set `TELEGRAM_ALLOWED_USERS` to restrict access to your Telegram user ID.
- Never expose the bot token publicly. Keep `.env` out of version control.
