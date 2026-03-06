#!/usr/bin/env python3
"""Integration-level smoke check for Telegram handler wiring."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

import telegram_bot


REQUIRED_COMMANDS = {
    "start",
    "clear",
    "new",
    "switch",
    "sessions",
    "status",
    "cd",
    "session",
    "kill",
    "delete",
    "sync",
    "topic",
    "history",
}

REQUIRED_MESSAGE_CALLBACKS = {
    "handle_message",
    "handle_unknown_command",
    "handle_file",
}


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    app = telegram_bot.build_application()
    handlers = [h for group_handlers in app.handlers.values() for h in group_handlers]

    commands = set()
    for handler in handlers:
        if isinstance(handler, CommandHandler):
            commands.update(handler.commands)

    missing_commands = sorted(REQUIRED_COMMANDS - commands)
    expect(not missing_commands, f"missing command handlers: {missing_commands}")

    ask_callback_registered = False
    for handler in handlers:
        if not isinstance(handler, CallbackQueryHandler):
            continue
        pattern = getattr(handler, "pattern", None)
        pattern_text = getattr(pattern, "pattern", str(pattern)) if pattern is not None else ""
        if pattern_text == "^ask:":
            ask_callback_registered = True
            break
    expect(ask_callback_registered, "missing AskUser callback handler")

    message_callbacks = {
        getattr(handler.callback, "__name__", "")
        for handler in handlers
        if isinstance(handler, MessageHandler)
    }
    missing_message_callbacks = sorted(REQUIRED_MESSAGE_CALLBACKS - message_callbacks)
    expect(
        not missing_message_callbacks,
        f"missing message handlers: {missing_message_callbacks}",
    )

    print("smoke_bot_routing: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
