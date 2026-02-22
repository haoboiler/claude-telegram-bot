# claude-telegram-bot

Telegram Bot that acts as a remote interface to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI, enabling mobile control of Claude Code via Telegram messages.

## Features

- **Multi-Session**: Run multiple independent Claude sessions in parallel (`/new`, `/switch`, `@name msg`)
- **Real-Time Activity**: Stream-JSON output shows what Claude is doing (Reading files, Running commands, etc.)
- **Heartbeat**: Updates "Thinking..." message every 30s with elapsed time and current activity
- **Stall Detection**: Auto-kills stuck processes if no output for 5 minutes
- **Session Routing**: Send messages to specific sessions with `@session_name your message`
- **Message Queue**: Per-session locking with queue position feedback

## Architecture

```
User (Telegram)
  -> Bot (python-telegram-bot, concurrent_updates=True)
    -> resolve session target (@name prefix or active session)
    -> per-session asyncio.Lock (different sessions run in parallel)
    -> claude -p --output-format stream-json --verbose
      -> real-time activity parsing
      -> heartbeat updates every 30s
      -> stall detection (kill if no events for 5 min)
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
| `CLAUDE_TIMEOUT` | `1800` | Total timeout per call (seconds) |
| `HEARTBEAT_INTERVAL` | `30` | Activity update interval (seconds) |
| `STALL_TIMEOUT` | `300` | Kill if no output for N seconds |
| `CLAUDE_MAX_TURNS` | `30` | Max agentic turns per call |

### 4. Run

```bash
# Foreground
python3 telegram_bot.py

# Background
nohup python3 telegram_bot.py > telegram_bot.log 2>&1 &
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
| `@name msg` | Send to specific session without switching |

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
