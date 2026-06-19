#!/usr/bin/env python3
"""
Telegram Bot for Claude Code remote interaction.
Allows controlling Claude Code via Telegram messages from your phone.

Uses Claude Agent SDK for structured communication with Claude Code,
including AskUserQuestion support via Telegram inline keyboards.
"""

import asyncio
import atexit
import functools
import logging
import os
import re
import signal
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.claude_telegram_bot.bootstrap.settings import (
    build_runtime_paths,
    load_runtime_env,
    parse_instance_name,
)
from src.claude_telegram_bot.bootstrap.auth_state import (
    resolve_auth_bootstrap_state,
)
from src.claude_telegram_bot.application.use_cases.session_commands import (
    run_attach_session_use_case,
    run_browse_use_case,
    run_cd_use_case,
    run_clear_session_use_case,
    run_delete_session_use_case,
    run_history_resolve_use_case,
    run_kill_use_case,
    run_new_session_use_case,
    run_session_info_use_case,
    run_sessions_use_case,
    run_sync_finish_use_case,
    run_sync_init_use_case,
    run_status_use_case,
    run_switch_session_use_case,
    run_topic_use_case,
)
from src.claude_telegram_bot.infrastructure.claude.session_lookup import (
    check_active_terminal_session,
    find_sdk_session,
    list_project_dirs,
    list_sdk_sessions,
    validate_sdk_session_id,
)
from src.claude_telegram_bot.application.use_cases.ask_user import (
    parse_ask_callback_data,
    resolve_ask_selection,
)
from src.claude_telegram_bot.application.use_cases.file_flow import (
    run_prepare_file_caption_use_case,
)
from src.claude_telegram_bot.application.use_cases.message_flow import (
    render_busy_reply,
    render_final_activity_reply,
    render_progress_reply,
    run_label_response_parts_use_case,
    run_prepare_message_use_case,
)
from src.claude_telegram_bot.domain.policies.group_auth import (
    is_group_chat as _is_group_chat_policy,
    should_respond_in_group as _should_respond_in_group_policy,
)

PROJECT_ROOT = Path(__file__).parent
INSTANCE_NAME = parse_instance_name()
RUNTIME_PATHS = build_runtime_paths(PROJECT_ROOT, INSTANCE_NAME)
load_runtime_env(RUNTIME_PATHS)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest as TelegramBadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode, ChatAction

from claude_agent_sdk import PermissionResultAllow
from src.claude_telegram_bot.infrastructure.claude.gateway import (
    ClaudeGateway,
    ClaudeGatewayConfig,
    UsageInfo,
)
from src.claude_telegram_bot.infrastructure.claude.history import (
    format_history_message,
    read_session_history,
)
from src.claude_telegram_bot.infrastructure.persistence.bot_state import (
    load_state as _load_state_impl,
    save_state as _save_state_impl,
)
from src.claude_telegram_bot.infrastructure.persistence.memory.session_repository import (
    InMemorySessionRepository,
)
from src.claude_telegram_bot.infrastructure.persistence.sqlite.session_repository import (
    SqliteSessionRepository,
)
from src.claude_telegram_bot.infrastructure.telegram.messages import (
    extract_sendable_files as _extract_sendable_files_impl,
    send_files_to_chat as _send_files_to_chat_impl,
    split_message,
)
from src.claude_telegram_bot.infrastructure.telegram.uploads import (
    resolve_upload_path,
    select_upload_file,
)

# Remove CLAUDECODE env var to prevent nested session detection
# (bot may be launched from within a Claude Code session)
os.environ.pop("CLAUDECODE", None)

# ─── Configuration ───────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required. "
                       "Set it in .env or export it before running.")

# Restrict to your Telegram user ID (set after first /start)
ALLOWED_USER_IDS_ENV = os.environ.get("TELEGRAM_ALLOWED_USERS", "")

# Working directory for claude CLI
WORK_DIR = os.environ.get("CLAUDE_WORK_DIR", os.getcwd())

# Claude CLI timeout (seconds) - 0 means no timeout, rely on --max-turns and /kill
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "0"))

# Heartbeat interval: send "still working" update every N seconds
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))

# Stall warning: warn user (not kill) if no new events for this many seconds
STALL_WARN_TIMEOUT = int(os.environ.get("STALL_WARN_TIMEOUT", "300"))

# Max agentic turns to prevent infinite loops
MAX_TURNS = int(os.environ.get("CLAUDE_MAX_TURNS", "120"))

# AskUserQuestion timeout (seconds) - how long to wait for user to answer
ASK_USER_TIMEOUT = int(os.environ.get("ASK_USER_TIMEOUT", "300"))

# Upload directory for files received from Telegram
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(WORK_DIR, "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Session repository backend: memory (default) or sqlite.
_DEFAULT_SQLITE_REPO_PATH = (
    str(RUNTIME_PATHS.project_root / "instances" / f"{INSTANCE_NAME}.sessions.sqlite")
    if INSTANCE_NAME
    else str(RUNTIME_PATHS.project_root / ".sessions.sqlite")
)
SESSION_REPO_BACKEND = os.environ.get("SESSION_REPO_BACKEND", "memory").strip().lower()
SESSION_REPO_SQLITE_PATH = os.environ.get(
    "SESSION_REPO_SQLITE_PATH",
    _DEFAULT_SQLITE_REPO_PATH,
)

# SSH SOCKS proxy relay for Telegram API (optional).
# Set to an SSH host alias (e.g. "aws-proxy") to tunnel all Telegram traffic
# through that host via SOCKS5. Leave empty to connect directly.
SSH_PROXY_RELAY = os.environ.get("SSH_PROXY_RELAY", "").strip()
_SOCKS_PROXY_URL: Optional[str] = None


class _SSHTunnelManager:
    """Manages an SSH SOCKS5 tunnel with automatic restart on failure."""

    def __init__(self, relay_host: str, local_port: int):
        self.relay_host = relay_host
        self.local_port = local_port
        self.process: Optional[subprocess.Popen] = None
        self._lock = __import__("threading").Lock()

    @property
    def ssh_cmd(self) -> list[str]:
        return [
            "ssh", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-N", "-D", f"127.0.0.1:{self.local_port}",
            self.relay_host,
        ]

    def start(self) -> None:
        with self._lock:
            if self.process and self.process.poll() is None:
                return
            self.process = subprocess.Popen(
                self.ssh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            # Wait for SSH handshake to complete; check periodically
            import time as _time
            for _ in range(10):
                _time.sleep(0.5)
                if self.process.poll() is not None:
                    stderr = self.process.stderr.read().decode() if self.process.stderr else ""
                    raise RuntimeError(
                        f"SSH tunnel to {self.relay_host} failed: {stderr}"
                    )
                # Verify the port is actually listening
                import socket as _sock_mod
                try:
                    with _sock_mod.create_connection(("127.0.0.1", self.local_port), timeout=0.3):
                        return  # tunnel is ready
                except OSError:
                    continue
            # If we get here, tunnel process is alive but port not ready
            raise RuntimeError(
                f"SSH tunnel to {self.relay_host}: port {self.local_port} not ready after 5s"
            )

    def ensure_alive(self) -> None:
        """Restart tunnel if it has died."""
        with self._lock:
            if self.process and self.process.poll() is None:
                return
        # Log outside lock to avoid import-time issues
        _tunnel_log = __import__("logging").getLogger("claude-tg-bot")
        _tunnel_log.warning("SSH proxy tunnel died, restarting...")
        self.start()
        _tunnel_log.info(f"SSH tunnel restarted (pid: {self.process.pid})")

    def stop(self) -> None:
        with self._lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()


_ssh_tunnel: Optional[_SSHTunnelManager] = None

if SSH_PROXY_RELAY:
    import socket as _socket
    _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _sock.bind(("127.0.0.1", 0))
    _socks_port = _sock.getsockname()[1]
    _sock.close()

    _ssh_tunnel = _SSHTunnelManager(SSH_PROXY_RELAY, _socks_port)
    _ssh_tunnel.start()
    _SOCKS_PROXY_URL = f"socks5://127.0.0.1:{_socks_port}"

    atexit.register(_ssh_tunnel.stop)

    _original_sigterm = signal.getsignal(signal.SIGTERM)
    def _sigterm_handler(signum, frame):
        _ssh_tunnel.stop()
        if callable(_original_sigterm) and _original_sigterm not in (signal.SIG_DFL, signal.SIG_IGN):
            _original_sigterm(signum, frame)
        else:
            raise SystemExit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

# Telegram message max length
TG_MAX_LEN = 4000

# ─── Network retry helper (for SSH tunnel resilience) ────────────────────────

_TG_SEND_MAX_RETRIES = 3
_TG_SEND_RETRY_DELAY = 2.0  # seconds between retries


async def _tg_retry(coro_factory, retries=_TG_SEND_MAX_RETRIES):
    """
    Call a Telegram API coroutine with retry on NetworkError.

    Usage:
        msg = await _tg_retry(lambda: update.message.reply_text("hello"))

    If SSH tunnel is configured and a NetworkError occurs, ensures the tunnel
    is alive before retrying.
    """
    from telegram.error import NetworkError
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_factory()
        except NetworkError as e:
            last_exc = e
            if attempt == retries:
                raise
            _retry_log = __import__("logging").getLogger("claude-tg-bot")
            _retry_log.warning(
                f"Telegram NetworkError (attempt {attempt}/{retries}): {e}"
            )
            if _ssh_tunnel:
                try:
                    _ssh_tunnel.ensure_alive()
                except Exception as te:
                    _retry_log.error(f"Tunnel restart failed during retry: {te}")
            await asyncio.sleep(_TG_SEND_RETRY_DELAY)
    raise last_exc  # type: ignore[misc]

# Memory-notebook auto-sync script path
AUTO_SYNC_SCRIPT = os.path.expanduser(
    "~/.claude/skills/memory-notebook/scripts/auto-sync.sh"
)

# ─── Persistent state (owner auto-register survives restarts) ────────────────

# State file path: instances/<name>.state.yaml (or .bot-state.yaml for default)
_STATE_FILE = RUNTIME_PATHS.state_file_path


def _load_state() -> dict:
    """Load persisted bot state (owner_id, allowed_users) from state file."""
    return _load_state_impl(_STATE_FILE)


def _save_state(owner_id: int | None, allowed_ids: set[int]) -> None:
    """Persist bot state to state file."""
    _save_state_impl(_STATE_FILE, owner_id, allowed_ids, logger=log)


# ─── Group Mode Configuration ────────────────────────────────────────────────

_auth_bootstrap = resolve_auth_bootstrap_state(
    owner_env_value=os.environ.get("TELEGRAM_OWNER_ID", ""),
    allowed_env_value=ALLOWED_USER_IDS_ENV,
    persisted_state=_load_state(),
)
OWNER_USER_ID: int | None = _auth_bootstrap.owner_user_id
ALLOWED_USER_IDS: set[int] = set(_auth_bootstrap.allowed_user_ids)
if _auth_bootstrap.loaded_owner_from_state:
    print(f"[state] Loaded owner from {_STATE_FILE}: {OWNER_USER_ID}")
if _auth_bootstrap.loaded_allowed_from_state:
    print(f"[state] Loaded allowed users from {_STATE_FILE}: {ALLOWED_USER_IDS}")

# Bot's own username (set dynamically at startup via getMe)
BOT_USERNAME: str = ""

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("claude-tg-bot")

# ─── Session & Lock management ──────────────────────────────────────────────

def get_topic_id(update: Update) -> int:
    """Extract topic_id from update for session scoping.

    In a forum group, each topic has a unique message_thread_id.
    In private chats or non-forum groups, returns 0.
    This allows sessions to be scoped per-topic in forum groups
    while maintaining backward compatibility with private chats.
    """
    msg = update.effective_message
    if msg and getattr(msg, "is_topic_message", False) and msg.message_thread_id:
        topic_id = int(msg.message_thread_id)
        topic_created = getattr(msg, "forum_topic_created", None)
        if topic_created and getattr(topic_created, "name", None):
            topic_names[topic_id] = topic_created.name
        return topic_id
    return 0


def _thread_id_or_none(topic_id: int) -> int | None:
    """Convert topic_id to message_thread_id parameter (None if 0)."""
    return topic_id if topic_id else None


# Session repository backend selection.
if SESSION_REPO_BACKEND == "sqlite":
    SESSION_REPO = SqliteSessionRepository(SESSION_REPO_SQLITE_PATH)
    log.info(f"Session repository backend: sqlite ({SESSION_REPO_SQLITE_PATH})")
elif SESSION_REPO_BACKEND == "memory":
    SESSION_REPO = InMemorySessionRepository()
    log.info("Session repository backend: memory")
else:
    log.warning(
        "Unknown SESSION_REPO_BACKEND=%s, falling back to memory",
        SESSION_REPO_BACKEND,
    )
    SESSION_REPO = InMemorySessionRepository()
    log.info("Session repository backend: memory")

# Keep backward-compatible global names as references to repository state maps.
# Existing command handlers and helper functions can continue to use these names
# without behavior change while we progressively migrate call sites.
topic_active_session = SESSION_REPO.topic_active_session
topic_all_sessions = SESSION_REPO.topic_all_sessions
session_sdk_ids = SESSION_REPO.session_sdk_ids
session_locks = SESSION_REPO.session_locks
session_pending = SESSION_REPO.session_pending
session_clients = SESSION_REPO.session_clients
topic_session_counter = SESSION_REPO.topic_session_counter
topic_names = SESSION_REPO.topic_names
session_work_dirs = SESSION_REPO.session_work_dirs
session_cwd_locked = SESSION_REPO.session_cwd_locked

# Project shortname -> full path mapping
# Fallback shortcuts (used if shortcuts.yaml is missing or parse fails)
_FALLBACK_SHORTCUTS: dict[str, str] = {
    "tgcc": "/home/gkh/claude_tasks/claude-telegram-bot",
    "ashare": "/home/gkh/ashare",
    "tmp": "/home/gkh/claude_tasks/tmp_task",
}

SHORTCUTS_YAML_PATH = os.path.expanduser("~/.claude/shortcuts.yaml")
_shortcuts_cache: dict[str, str] | None = None
_shortcuts_mtime: float = 0.0


def _load_shortcuts_from_yaml() -> dict[str, str]:
    """Load project shortcuts from ~/.claude/shortcuts.yaml.

    Reads 'shortcuts' (name -> path) and 'aliases' (alias -> shortcut name),
    merging aliases into the final mapping.
    Returns fallback shortcuts if YAML is missing or parse fails.
    """
    try:
        import yaml
        with open(SHORTCUTS_YAML_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return dict(_FALLBACK_SHORTCUTS)

    if not isinstance(data, dict):
        return dict(_FALLBACK_SHORTCUTS)

    shortcuts: dict[str, str] = {}
    # Load direct shortcuts
    raw_shortcuts = data.get("shortcuts", {})
    if isinstance(raw_shortcuts, dict):
        for name, path in raw_shortcuts.items():
            if name and path:
                shortcuts[str(name)] = str(path)

    # Resolve aliases (alias -> shortcut name -> path)
    raw_aliases = data.get("aliases", {})
    if isinstance(raw_aliases, dict):
        for alias, target in raw_aliases.items():
            if alias and target and str(target) in shortcuts:
                shortcuts[str(alias)] = shortcuts[str(target)]

    return shortcuts if shortcuts else dict(_FALLBACK_SHORTCUTS)


def get_project_shortcuts() -> dict[str, str]:
    """Get project shortcuts, reloading from shortcuts.yaml if file changed."""
    global _shortcuts_cache, _shortcuts_mtime
    try:
        mtime = os.path.getmtime(SHORTCUTS_YAML_PATH)
    except OSError:
        mtime = 0.0
    if _shortcuts_cache is None or mtime != _shortcuts_mtime:
        _shortcuts_cache = _load_shortcuts_from_yaml()
        _shortcuts_mtime = mtime
        logging.info("Reloaded project shortcuts from shortcuts.yaml: %s", list(_shortcuts_cache.keys()))
    return _shortcuts_cache

# Pending AskUserQuestion futures: question_id -> asyncio.Future
pending_questions: dict[str, asyncio.Future] = {}
# question_id -> list of original options (for resolving callback index)
pending_question_options: dict[str, list[dict]] = {}


def get_session_lock(session_id: str) -> asyncio.Lock:
    """Get or create a lock for a session."""
    return SESSION_REPO.get_session_lock(session_id)


def _next_default_name(topic_id: int) -> str:
    """Generate next default session name like s1, s2, ..."""
    return SESSION_REPO.next_default_name(topic_id)


def resolve_cwd(cwd_arg: Optional[str]) -> Optional[str]:
    """Resolve a cwd argument to an absolute path.

    Accepts: project shortname (e.g. 'casimir_ashare'), ~ path, absolute path,
    or relative path (resolved against global WORK_DIR).
    Returns absolute path if valid directory, None otherwise.
    """
    if not cwd_arg:
        return None

    # Check project shortcuts first (dynamically loaded from CLAUDE.md)
    shortcuts = get_project_shortcuts()
    if cwd_arg in shortcuts:
        path = shortcuts[cwd_arg]
    else:
        path = os.path.expanduser(cwd_arg)
        if not os.path.isabs(path):
            path = os.path.join(WORK_DIR, path)
        path = os.path.abspath(path)

    return path if os.path.isdir(path) else None


def get_session_cwd(session_id: str) -> str:
    """Get the working directory for a session (falls back to global WORK_DIR)."""
    return SESSION_REPO.get_session_cwd(session_id, WORK_DIR)


def get_or_create_session(topic_id: int) -> str:
    """Get existing session or create a new one. Returns local_session_id."""
    return SESSION_REPO.get_or_create_session(topic_id, logger=log)


def create_new_session(topic_id: int, name: Optional[str] = None,
                       cwd: Optional[str] = None) -> tuple[str, str]:
    """Create a new session and switch to it. Returns (name, local_session_id).

    Args:
        cwd: Optional per-session working directory (absolute path, already resolved).
             If None, inherits global WORK_DIR at call time.
    """
    return SESSION_REPO.create_new_session(topic_id, name, cwd, logger=log)


def switch_session(topic_id: int, name: str) -> Optional[str]:
    """Switch to an existing session by name. Returns session_id or None if not found."""
    return SESSION_REPO.switch_session(topic_id, name, logger=log)


def clear_session(topic_id: int, target_name: str | None = None) -> tuple[str, str, str | None]:
    """Clear a session's context and start fresh.

    Args:
        target_name: Session name to clear. If None, clears the active session.

    When target_name is None (clear active):
        Destroys current session, creates a new auto-named one, switches to it.
    When target_name is given:
        Resets the named session (new ID, preserves name and cwd).
        Does NOT change the active session.

    Returns: (new_session_name, new_sid, old_cwd).
    """
    return SESSION_REPO.clear_session(topic_id, target_name, logger=log)


def delete_session(topic_id: int, name: str) -> tuple[bool, str, str | None]:
    """Delete a specific session by name. Returns (success, message, deleted_cwd)."""
    return SESSION_REPO.delete_session(topic_id, name, WORK_DIR, logger=log)


async def run_auto_sync(cwd: str) -> str:
    """Run memory-notebook auto-sync in the given cwd. Returns status message."""
    if not os.path.isfile(AUTO_SYNC_SCRIPT):
        return "sync script not found"
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", AUTO_SYNC_SCRIPT,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            log.info(f"Auto-sync completed (cwd={cwd})")
            return "synced"
        else:
            err = stderr.decode().strip()
            log.warning(f"Auto-sync exited {proc.returncode}: {err}")
            return f"sync error (exit {proc.returncode})"
    except asyncio.TimeoutError:
        log.warning(f"Auto-sync timed out (cwd={cwd})")
        try:
            proc.kill()
        except Exception:
            pass
        return "sync timed out"
    except Exception as e:
        log.exception(f"Auto-sync failed: {e}")
        return f"sync failed: {e}"


def list_sessions(topic_id: int) -> list[tuple[str, str, bool, bool]]:
    """List all sessions for a topic. Returns [(name, session_id, is_active, is_busy), ...]."""
    return SESSION_REPO.list_sessions(topic_id)


def resolve_session_target(topic_id: int, text: str) -> tuple[str, str, str]:
    """Parse @session_name prefix from message text.

    Returns (session_name, local_session_id, remaining_prompt).
    If no @prefix, uses active session.
    If @name doesn't exist, auto-creates it (inherits active session's cwd).
    """
    return SESSION_REPO.resolve_session_target(
        topic_id, text, WORK_DIR, logger=log
    )


# ─── Auth check ──────────────────────────────────────────────────────────────


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # No restriction if not configured
    return user_id in ALLOWED_USER_IDS


# ─── Group chat awareness ───────────────────────────────────────────────────

# Track which group chats have already received the Privacy Mode reminder
# (per chat_id, only remind once per bot lifetime)
_privacy_mode_reminded: set[int] = set()


async def _check_and_remind_privacy_mode(update: Update) -> None:
    """Send a one-time Privacy Mode reminder when bot first operates in a group.

    Telegram's Privacy Mode (ON by default) prevents bots from receiving
    regular messages in groups — they only see /commands and direct replies.
    This means @mention won't work until the owner disables it via @BotFather.
    """
    if not _is_group_chat(update):
        return
    chat_id = update.effective_chat.id
    if chat_id in _privacy_mode_reminded:
        return
    _privacy_mode_reminded.add(chat_id)
    topic_id = get_topic_id(update)

    await update.effective_chat.send_message(
        f"👋 *{BOT_USERNAME or 'Bot'} joined this group!*\n\n"
        f"⚠️ *Important: Disable Privacy Mode*\n"
        f"By default, Telegram's Privacy Mode is ON, which means "
        f"I can only see `/commands` and direct replies — "
        f"*@mentions won't work*.\n\n"
        f"To fix this:\n"
        f"1. Open @BotFather\n"
        f"2. Send `/mybots` → select `@{BOT_USERNAME}`\n"
        f"3. `Bot Settings` → `Group Privacy` → *Disabled*\n\n"
        f"After that, @mention and reply will both work.",
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=_thread_id_or_none(topic_id),
    )


def _is_group_chat(update: Update) -> bool:
    """Check if the message is from a group/supergroup chat."""
    return _is_group_chat_policy(update)


def should_respond_in_group(update: Update, is_command: bool = False) -> bool:
    """Determine if the bot should respond to this message in a group chat.

    In group chats, only respond if:
    1. Sender is the bot's owner (or authorized user), AND
    2. Message is directed at this bot (command, @mention, or reply to bot)

    With Privacy Mode OFF the bot receives ALL group messages, so we must
    filter carefully to avoid responding to messages meant for other bots.

    Always returns True for private chats.
    """
    return _should_respond_in_group_policy(
        update,
        is_command=is_command,
        owner_user_id=OWNER_USER_ID,
        allowed_user_ids=ALLOWED_USER_IDS,
        bot_username=BOT_USERNAME,
        logger=log,
    )


def _check_group_auth(update: Update, is_command: bool = False) -> bool | None:
    """Unified group/private auth check.

    Returns:
        True:  authorized (group owner or authorized private user)
        False: not authorized in group — silently ignore
        None:  not a group chat — caller should use existing is_authorized()
    """
    if not _is_group_chat(update):
        return None  # Not in group, caller uses existing logic
    return should_respond_in_group(update, is_command=is_command)


def _require_auth(fn=None, *, is_command: bool = True):
    """Decorator that adds authorization checks to Telegram handlers.

    Eliminates the repeated 4-line auth boilerplate from every handler.

    Usage::

        @_require_auth                     # is_command=True (silent reject)
        async def cmd_foo(update, ctx): ...

        @_require_auth(is_command=False)   # unauthorized → reply message
        async def handle_bar(update, ctx): ...
    """
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            group_auth = _check_group_auth(update, is_command=is_command)
            if group_auth is False:
                return
            if group_auth is None and not is_authorized(user_id):
                if not is_command:
                    await update.message.reply_text("Unauthorized. Send /start first.")
                return
            return await handler(update, ctx)
        return wrapper
    # Support both @_require_auth and @_require_auth(is_command=False)
    if fn is not None:
        return decorator(fn)
    return decorator


async def _send_result(update: Update, result) -> object:
    """Send a use-case result to the user, respecting its ``parse_mode``.

    Returns the sent ``Message`` object (useful when callers need to
    edit the message later, e.g. for auto-sync status updates).

    Falls back to plain text if Markdown parsing fails.
    """
    pm = ParseMode.MARKDOWN if result.parse_mode == "Markdown" else None
    try:
        return await update.message.reply_text(result.reply_text, parse_mode=pm)
    except TelegramBadRequest:
        # Markdown parse failure — retry as plain text
        return await update.message.reply_text(result.reply_text, parse_mode=None)


def extract_reply_context(update: Update) -> str | None:
    """Extract quoted message context when user replies to another bot/user message.

    Returns formatted context string to prepend to the prompt,
    or None if not a relevant reply.
    """
    msg = update.message or update.effective_message
    if not msg or not msg.reply_to_message:
        return None

    replied = msg.reply_to_message

    # Skip if replying to own bot's message (normal conversation flow)
    if replied.from_user and BOT_USERNAME:
        if replied.from_user.username == BOT_USERNAME:
            return None

    # Build sender identity
    sender_name = "Unknown"
    if replied.from_user:
        if replied.from_user.username:
            sender_name = f"@{replied.from_user.username}"
        elif replied.from_user.full_name:
            sender_name = replied.from_user.full_name

    quoted_text = replied.text or replied.caption or "[non-text message]"

    # Truncate long quoted text to save input tokens
    max_quote_len = 300
    if len(quoted_text) > max_quote_len:
        quoted_text = quoted_text[:max_quote_len] + "...(truncated)"

    return (
        f"[Quoted message from {sender_name}]\n"
        f"```\n{quoted_text}\n```"
    )


def strip_bot_mention(text: str) -> str:
    """Remove @bot_username mention from message text."""
    if BOT_USERNAME:
        text = re.sub(rf"@{re.escape(BOT_USERNAME)}\b", "", text).strip()
    return text


async def _update_thinking_msg(thinking_msg, activity_log: list[str],
                               session_name: str, elapsed: float):
    """Update the thinking message with current activity log (throttled by caller)."""
    if not thinking_msg:
        return
    mins, secs = divmod(int(elapsed), 60)
    time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
    label = f"[{session_name}] " if session_name else ""
    header = f"{label}Working... ({time_str})\n"
    recent = activity_log[-50:]
    dropped = len(activity_log) - len(recent)
    # Trim oldest of the recent window if it would exceed Telegram's limit.
    lines = [f"  ▸ {a}" for a in recent]
    log_text = "\n".join(lines)
    while lines and len(header) + len(log_text) > 3500:
        lines.pop(0)
        dropped += 1
        log_text = "\n".join(lines)
    if dropped > 0:
        log_text = f"  ... ({dropped} earlier)\n" + log_text
    try:
        await thinking_msg.edit_text(f"{header}{log_text}")
    except Exception:
        pass


# ─── AskUserQuestion handling ───────────────────────────────────────────────


def _make_can_use_tool(chat, session_name: str, topic_id: int):
    """Create a can_use_tool callback that forwards AskUserQuestion to Telegram."""

    async def can_use_tool(tool_name, tool_input, context):
        if tool_name == "AskUserQuestion":
            return await _handle_ask_user_question(tool_input, chat, session_name, topic_id)
        # Allow all other tools
        return PermissionResultAllow(updated_input=tool_input)

    return can_use_tool


async def _handle_ask_user_question(tool_input: dict, chat, session_name: str, topic_id: int):
    """Forward AskUserQuestion to Telegram and wait for user response."""
    questions = tool_input.get("questions", [])
    if not questions:
        return PermissionResultAllow(updated_input=tool_input)

    answers = {}
    label = f"[{session_name}] " if session_name else ""

    for q in questions:
        question_text = q.get("question", "")
        header = q.get("header", "")
        options = q.get("options", [])
        multi_select = q.get("multiSelect", False)

        # Generate unique question ID
        qid = str(uuid.uuid4())[:8]

        # Build message text
        text_parts = [f"{label}Agent is asking:"]
        if header:
            text_parts.append(f"[{header}]")
        text_parts.append(f"\n{question_text}\n")
        for i, opt in enumerate(options):
            desc = opt.get("description", "")
            text_parts.append(f"  {i+1}. {opt['label']}" + (f" — {desc}" if desc else ""))

        msg_text = "\n".join(text_parts)

        # Build inline keyboard
        keyboard = []
        for i, opt in enumerate(options):
            keyboard.append([InlineKeyboardButton(
                opt["label"],
                callback_data=f"ask:{qid}:{i}",
            )])
        # "Other" option - user types a reply
        keyboard.append([InlineKeyboardButton(
            "Other (type reply)",
            callback_data=f"ask:{qid}:other",
        )])
        markup = InlineKeyboardMarkup(keyboard)

        # Create future and register it
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        pending_questions[qid] = future
        pending_question_options[qid] = options

        try:
            await chat.send_message(
                msg_text,
                reply_markup=markup,
                message_thread_id=_thread_id_or_none(topic_id),
            )
            log.info(f"AskUserQuestion forwarded to Telegram: qid={qid}, question={question_text[:50]}")

            # Wait for user to answer (with timeout)
            answer = await asyncio.wait_for(future, timeout=ASK_USER_TIMEOUT)
            answers[question_text] = answer
            log.info(f"AskUserQuestion answered: qid={qid}, answer={answer}")
        except asyncio.TimeoutError:
            # Timeout - auto-select first option
            fallback = options[0]["label"] if options else "Yes"
            answers[question_text] = fallback
            log.warning(f"AskUserQuestion timeout: qid={qid}, auto-selected={fallback}")
            try:
                await chat.send_message(
                    f"{label}No answer in {ASK_USER_TIMEOUT}s, auto-selected: {fallback}",
                    message_thread_id=_thread_id_or_none(topic_id),
                )
            except Exception:
                pass
        finally:
            pending_questions.pop(qid, None)
            pending_question_options.pop(qid, None)

    return PermissionResultAllow(
        updated_input={"questions": questions, "answers": answers}
    )


async def handle_ask_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for AskUserQuestion."""
    query = update.callback_query
    if not query:
        return

    parsed = parse_ask_callback_data(query.data or "")
    if not parsed:
        await query.answer("Invalid callback data")
        return

    qid, choice = parsed
    future = pending_questions.get(qid)

    if not future or future.done():
        await query.answer("Question already answered or expired.")
        return

    if choice == "other":
        # Mark that we're waiting for a text reply
        # Store a special marker so handle_message can pick it up
        await query.answer()
        await query.message.reply_text(
            "Type your answer as a reply to this message:")
        # Store the qid in a way that handle_text_answer can find it
        # We use a simple dict mapping chat_id to pending qid
        _pending_text_answers[query.message.chat_id] = qid
        return

    options = pending_question_options.get(qid, [])
    selected = resolve_ask_selection(choice, options)

    future.set_result(selected)
    await query.answer(f"Selected: {selected}")

    # Update the message to show the selection
    try:
        await query.message.edit_text(
            query.message.text + f"\n\n-> {selected}")
    except Exception:
        pass


# Track pending "Other" text answers: chat_id -> qid
_pending_text_answers: dict[int, str] = {}


async def _check_text_answer(chat_id: int, text: str) -> bool:
    """Check if a text message is an answer to a pending AskUserQuestion 'Other'.
    Returns True if it was consumed as an answer."""
    qid = _pending_text_answers.pop(chat_id, None)
    if not qid:
        return False

    future = pending_questions.get(qid)
    if future and not future.done():
        future.set_result(text)
        return True
    return False


# ─── Claude SDK interaction ─────────────────────────────────────────────────

_CLAUDE_GATEWAY: ClaudeGateway | None = None


def _get_claude_gateway() -> ClaudeGateway:
    global _CLAUDE_GATEWAY
    if _CLAUDE_GATEWAY is None:
        _CLAUDE_GATEWAY = ClaudeGateway(
            config=ClaudeGatewayConfig(
                max_turns=MAX_TURNS,
                timeout_seconds=CLAUDE_TIMEOUT,
                heartbeat_interval=HEARTBEAT_INTERVAL,
                stall_warn_timeout=STALL_WARN_TIMEOUT,
            ),
            session_sdk_ids=session_sdk_ids,
            session_clients=session_clients,
            get_session_cwd=get_session_cwd,
            make_can_use_tool=_make_can_use_tool,
            update_thinking_msg=_update_thinking_msg,
            thread_id_or_none=_thread_id_or_none,
            logger=log,
        )
    return _CLAUDE_GATEWAY


async def call_claude(prompt: str, session_id: str,
                      thinking_msg=None, chat=None,
                      session_name: str = "",
                      topic_id: int = 0):
    """Call Claude via gateway. Returns (response_text, activity_log, usage_info)."""
    return await _get_claude_gateway().call(
        prompt=prompt,
        session_id=session_id,
        thinking_msg=thinking_msg,
        chat=chat,
        session_name=session_name,
        topic_id=topic_id,
    )


# ─── File auto-send helpers (migrated to infrastructure module) ─────────────


def extract_sendable_files(text: str) -> list[tuple[str, bool]]:
    """Backward-compatible wrapper around infrastructure file extraction."""
    return _extract_sendable_files_impl(text, WORK_DIR)


async def send_files_to_chat(
    chat,
    files: list[tuple[str, bool]],
    session_name: str = "",
    topic_id: int = 0,
):
    """Backward-compatible wrapper around infrastructure file sending."""
    await _send_files_to_chat_impl(
        chat=chat,
        files=files,
        session_name=session_name,
        topic_thread_id=_thread_id_or_none(topic_id),
        logger=log,
    )


# ─── Telegram handlers ───────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    global OWNER_USER_ID
    user = update.effective_user
    user_id = user.id
    topic_id = get_topic_id(update)

    # ── Auto-register: first /start user becomes owner ──
    # This runs BEFORE the normal auth check so the very first user can register.
    # Works in both private chat and group chat. Persisted to state file.
    if not OWNER_USER_ID and not ALLOWED_USER_IDS:
        OWNER_USER_ID = user_id
        ALLOWED_USER_IDS.add(user_id)
        _save_state(OWNER_USER_ID, ALLOWED_USER_IDS)
        log.info(f"Auto-registered owner: {user.full_name} (ID: {user_id})")

    # ── Standard auth ──
    group_auth = _check_group_auth(update, is_command=True)
    if group_auth is False:
        return

    # First time in this group? Remind about Privacy Mode
    if _is_group_chat(update):
        await _check_and_remind_privacy_mode(update)

    # Private chat auto-register (fallback, e.g. OWNER set in env but list empty)
    if group_auth is None and not ALLOWED_USER_IDS:
        ALLOWED_USER_IDS.add(user_id)
        _save_state(OWNER_USER_ID, ALLOWED_USER_IDS)
        log.info(f"Auto-registered user: {user.full_name} (ID: {user_id})")

    if group_auth is None and not is_authorized(user_id):
        await update.message.reply_text("Unauthorized.")
        return

    session_id = get_or_create_session(topic_id)
    active_name = topic_active_session.get(topic_id, "?")
    await update.message.reply_text(
        f"Claude Code Bot ready!\n\n"
        f"Your ID: `{user_id}`\n"
        f"Session: `{active_name}` (`{session_id[:8]}...`)\n"
        f"Working dir: `{WORK_DIR}`\n\n"
        f"Commands:\n"
        f"/new [name] [cwd] - New session (keeps old)\n"
        f"/switch <name> - Switch session\n"
        f"/sessions - List all sessions\n"
        f"/clear [name] - Reset session context\n"
        f"/delete <name> - Delete a session\n"
        f"/status - Bot status\n"
        f"/topic [info|list] - Topic session info\n"
        f"/cd [path|shortname] - Change session cwd\n"
        f"/session - Current session info\n"
        f"/kill [name] - Kill stuck session\n"
        f"/history [N] [name] - Show session history\n"
        f"/sync - Sync memory notebook\n\n"
        f"Send any message to interact with Claude.\n"
        f"Use `@name msg` to send to a specific session.\n"
        f"Different sessions run in parallel!\n\n"
        f"In a forum group, each topic has its own session space.\n\n"
        f"Per-session cwd: use `/new build casimir_ashare`\n"
        f"to create a session bound to a project.",
        parse_mode=ParseMode.MARKDOWN,
    )


@_require_auth
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear [name] - reset a session's conversation context.

    Usage:
        /clear           - clear active session (destroys it, creates new auto-named)
        /clear casimir   - clear named session (keeps name and cwd, resets context)
    """
    topic_id = get_topic_id(update)
    result = run_clear_session_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        topic_all_sessions=topic_all_sessions,
        clear_session=clear_session,
        get_session_cwd=get_session_cwd,
    )
    msg = await _send_result(update, result)
    if not result.ok:
        return

    # Auto-sync memory notebook from the old session's cwd
    if result.old_cwd:
        sync_status = await run_auto_sync(result.old_cwd)
        if sync_status == "synced":
            try:
                await msg.edit_text(
                    result.reply_text + "\n📓 Memory notebook synced.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass


@_require_auth
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete <name> - delete a specific session."""
    topic_id = get_topic_id(update)
    result = run_delete_session_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        delete_session=delete_session,
        topic_active_session=topic_active_session,
        topic_all_sessions=topic_all_sessions,
    )
    msg = await _send_result(update, result)
    if not result.ok:
        return

    # Auto-sync memory notebook from the deleted session's cwd
    if result.deleted_cwd:
        sync_status = await run_auto_sync(result.deleted_cwd)
        if sync_status == "synced":
            try:
                await msg.edit_text(
                    result.reply_text + "\n📓 Memory notebook synced.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass


@_require_auth
async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sync - manually trigger memory notebook sync."""
    topic_id = get_topic_id(update)

    sync_init = run_sync_init_use_case(
        topic_id=topic_id,
        topic_active_session=topic_active_session,
        topic_all_sessions=topic_all_sessions,
        get_session_cwd=get_session_cwd,
        work_dir=WORK_DIR,
    )
    msg = await update.message.reply_text(
        sync_init.initial_reply_text,
        parse_mode=ParseMode.MARKDOWN,
    )

    sync_status = await run_auto_sync(sync_init.cwd)
    finish_result = run_sync_finish_use_case(sync_status, sync_init.display_cwd)

    try:
        await msg.edit_text(
            finish_result.reply_text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        fallback_text = finish_result.reply_text.split("\n", 1)[0]
        await update.message.reply_text(fallback_text)


@_require_auth
async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new [name] [cwd] - create a new session and switch to it.

    Usage:
        /new                     - auto-named, uses global WORK_DIR
        /new build               - named 'build', uses global WORK_DIR
        /new build casimir_ashare - named 'build', cwd = project shortname
        /new build ~/ashare      - named 'build', cwd = expanded path
    """
    topic_id = get_topic_id(update)
    result = run_new_session_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        resolve_cwd=resolve_cwd,
        get_project_shortcuts=get_project_shortcuts,
        create_new_session=create_new_session,
        get_session_cwd=get_session_cwd,
    )
    await _send_result(update, result)


@_require_auth
async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /switch <name> - switch to an existing session."""
    topic_id = get_topic_id(update)
    result = run_switch_session_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        list_sessions=list_sessions,
        switch_session=switch_session,
        topic_all_sessions=topic_all_sessions,
    )
    await _send_result(update, result)


@_require_auth
async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sessions - list all sessions."""
    topic_id = get_topic_id(update)
    result = run_sessions_use_case(
        topic_id=topic_id,
        list_sessions=list_sessions,
        session_sdk_ids=session_sdk_ids,
        get_session_cwd=get_session_cwd,
    )
    await _send_result(update, result)


@_require_auth
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status - show bot status."""
    topic_id = get_topic_id(update)
    result = run_status_use_case(
        topic_id=topic_id,
        topic_all_sessions=topic_all_sessions,
        topic_active_session=topic_active_session,
        session_locks=session_locks,
        topic_names=topic_names,
        get_session_cwd=get_session_cwd,
        work_dir=WORK_DIR,
        timeout_seconds=CLAUDE_TIMEOUT,
        now_str=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    )
    await _send_result(update, result)


@_require_auth
async def cmd_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /topic [list|info] - topic management commands.

    Usage:
        /topic          - show current topic info (alias for /topic info)
        /topic info     - show current topic info with sessions
        /topic list     - list all known topics with session counts
    """
    topic_id = get_topic_id(update)
    result = run_topic_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        topic_all_sessions=topic_all_sessions,
        topic_active_session=topic_active_session,
        topic_names=topic_names,
        session_locks=session_locks,
        session_sdk_ids=session_sdk_ids,
        get_session_cwd=get_session_cwd,
    )
    await _send_result(update, result)


@_require_auth
async def cmd_cd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cd [path|shortname] - change working directory for current session.

    Usage:
        /cd                      - show current session's cwd
        /cd casimir_ashare       - change to project shortname
        /cd ~/ashare             - change to expanded path
        /cd --global <path>      - change global default WORK_DIR (affects new sessions)
    """
    global WORK_DIR
    topic_id = get_topic_id(update)
    result = run_cd_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        work_dir=WORK_DIR,
        topic_active_session=topic_active_session,
        topic_all_sessions=topic_all_sessions,
        session_work_dirs=session_work_dirs,
        session_cwd_locked=session_cwd_locked,
        get_session_cwd=get_session_cwd,
        resolve_cwd=resolve_cwd,
        get_project_shortcuts=get_project_shortcuts,
    )

    if result.new_global_work_dir is not None:
        WORK_DIR = result.new_global_work_dir
    if result.session_cwd_update is not None:
        sid, new_cwd = result.session_cwd_update
        session_work_dirs[sid] = new_cwd

    await _send_result(update, result)


@_require_auth
async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /session - show current session info."""
    topic_id = get_topic_id(update)
    result = run_session_info_use_case(
        topic_id=topic_id,
        topic_active_session=topic_active_session,
        topic_all_sessions=topic_all_sessions,
        session_sdk_ids=session_sdk_ids,
        session_work_dirs=session_work_dirs,
        session_cwd_locked=session_cwd_locked,
        get_session_cwd=get_session_cwd,
    )
    await _send_result(update, result)


@_require_auth
async def cmd_attach(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /attach <sdk_session_id> <name> - attach external Claude session.

    Usage:
        /attach e5f6a7b8-... myname   - attach SDK session with name "myname"
    """
    topic_id = get_topic_id(update)
    result = run_attach_session_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        topic_all_sessions=topic_all_sessions,
        find_sdk_session=find_sdk_session,
        check_active_terminal=check_active_terminal_session,
        validate_sdk_id=validate_sdk_session_id,
        session_sdk_ids=session_sdk_ids,
    )

    if result.ok:
        # Execute side effects: create session, bind SDK ID, lock cwd
        name, sid = SESSION_REPO.create_new_session(
            topic_id, name=result.session_name, cwd=result.cwd, logger=log,
        )
        session_sdk_ids[sid] = result.sdk_session_id
        session_cwd_locked[sid] = True
        log.info(
            f"Attached SDK session {result.sdk_session_id[:8]} "
            f"as '{name}' ({sid[:8]}) cwd={result.cwd}"
        )

    await _send_result(update, result)


@_require_auth
async def cmd_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /browse [project] [days] - browse SDK sessions on disk."""
    result = run_browse_use_case(
        args=list(ctx.args),
        list_project_dirs=list_project_dirs,
        list_sdk_sessions=list_sdk_sessions,
        session_sdk_ids=session_sdk_ids,
    )
    await _send_result(update, result)


@_require_auth
async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /kill [session_name] - interrupt a running Claude session."""
    topic_id = get_topic_id(update)
    decision = run_kill_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        topic_all_sessions=topic_all_sessions,
        topic_active_session=topic_active_session,
        session_clients=session_clients,
    )

    if not decision.should_interrupt:
        await _send_result(update, decision)
        return

    try:
        await decision.client.interrupt()
        log.info(f"User interrupted session {decision.target_name} ({decision.target_sid[:8]})")
        await update.message.reply_text(
            f"Interrupted session `{decision.target_name}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to interrupt: {e}")


@_require_auth
async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history [N] [session_name] - show recent session conversation.

    Usage:
        /history              - last 5 assistant messages of active session
        /history 10           - last 10 assistant messages of active session
        /history 10 ashare    - last 10 assistant messages of 'ashare' session
        /history ashare       - last 5 assistant messages of 'ashare' session
    """
    topic_id = get_topic_id(update)
    resolved = run_history_resolve_use_case(
        topic_id=topic_id,
        args=list(ctx.args),
        topic_all_sessions=topic_all_sessions,
        topic_active_session=topic_active_session,
        session_sdk_ids=session_sdk_ids,
    )
    if not resolved.ok:
        await _send_result(update, resolved)
        return

    n = resolved.n
    session_name = resolved.session_name
    session_id = resolved.session_id
    sdk_sid = resolved.sdk_sid

    # Get session cwd and read history
    cwd = get_session_cwd(session_id)

    try:
        turns = read_session_history(cwd, sdk_sid, n)
        output = format_history_message(turns, session_name, n)
    except FileNotFoundError as e:
        await update.message.reply_text(
            f"Session file not found for `{session_name}`.\n"
            f"Path: `{e}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except ValueError as e:
        await update.message.reply_text(
            f"No history found for `{session_name}`: {e}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as e:
        log.exception(f"Error reading history for {session_name}")
        await update.message.reply_text(f"Error reading history: {e}")
        return

    # Send via split_message (plain text to avoid Markdown escaping issues)
    parts = split_message(output)
    for part in parts:
        try:
            await update.message.reply_text(part)
        except Exception:
            # Fallback: truncate if even plain text fails
            await update.message.reply_text(part[:TG_MAX_LEN])


@_require_auth(is_command=False)
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file/photo/video uploads - download to server and optionally forward to Claude."""
    topic_id = get_topic_id(update)

    msg = update.message
    file_obj, original_name = select_upload_file(msg)

    if not file_obj:
        await msg.reply_text("Unsupported file type.")
        return

    # Download file
    try:
        tg_file = await file_obj.get_file()
        save_path = resolve_upload_path(UPLOAD_DIR, original_name)

        await tg_file.download_to_drive(save_path)
        log.info(f"File downloaded: {save_path} (from user {update.effective_user.id})")
    except Exception as e:
        log.exception("File download failed")
        await msg.reply_text(f"Download failed: {e}")
        return

    # Group chat input guard: require @session in caption to trigger Claude call.
    caption = msg.caption or ""
    if _is_group_chat(update) and caption and not caption.startswith("@"):
        # File saved but caption won't trigger Claude — just confirm download
        await msg.reply_text(f"File saved: `{save_path}`", parse_mode=ParseMode.MARKDOWN)
        return

    plan = run_prepare_file_caption_use_case(
        topic_id=topic_id,
        caption=caption,
        save_path=save_path,
        resolve_session_target=resolve_session_target,
    )

    # If there's a caption, forward file path + caption to Claude as a prompt
    if plan.forward_to_claude:
        session_name = plan.session_name or ""
        session_id = plan.session_id or ""
        prompt_text = plan.prompt_text or ""
        lock = get_session_lock(session_id)

        # Busy session policy: do not queue new requests while one is running.
        if lock.locked():
            await msg.reply_text(render_busy_reply(session_name, is_file_caption=True))
            return

        # Acquire per-session lock - different sessions can run in parallel
        async with lock:

            await msg.chat.send_action(
                ChatAction.TYPING,
                message_thread_id=_thread_id_or_none(topic_id),
            )
            thinking_msg = await msg.reply_text(
                render_progress_reply(session_name, is_file_caption=True)
            )

            response, activity_log, usage_info = await call_claude(
                prompt_text, session_id,
                thinking_msg=thinking_msg, chat=msg.chat,
                session_name=session_name,
                topic_id=topic_id,
            )

            # Finalize the activity log message with usage stats
            if thinking_msg:
                try:
                    final_text = render_final_activity_reply(session_name, activity_log)
                    usage_line = usage_info.summary_line()
                    if usage_line:
                        final_text += f"\n📊 {usage_line}"
                    await _tg_retry(lambda: thinking_msg.edit_text(final_text))
                except Exception:
                    pass

            parts = run_label_response_parts_use_case(
                session_name, split_message(response)
            )
            for labeled in parts:
                try:
                    await _tg_retry(lambda l=labeled: msg.reply_text(
                        l, parse_mode=ParseMode.MARKDOWN))
                except Exception:
                    await _tg_retry(lambda l=labeled: msg.reply_text(l))

            # Auto-send files/images found in response
            sendable = extract_sendable_files(response)
            if sendable:
                await _tg_retry(lambda: send_files_to_chat(
                    msg.chat, sendable, session_name, topic_id))
    else:
        # No caption - just confirm the file was saved
        await msg.reply_text(plan.reply_text, parse_mode=ParseMode.MARKDOWN)


# CLI built-in commands that don't work through the SDK.
# Map to natural language rewrites where possible, None = unsupported.
CLI_BUILTIN_REWRITES: dict[str, str | None] = {
    "skills": "List all your available skills with a brief description of each.",
    "help": None,
    "config": None,
    "login": None,
    "logout": None,
    "doctor": None,
    "compact": None,
    "model": None,
    "permissions": None,
    "cost": None,
}

# Slash commands that are allowed to be forwarded to Claude as skill invocations.
# Anything NOT in this set AND NOT in CLI_BUILTIN_REWRITES will be rejected locally.
SKILL_COMMAND_ALLOWLIST: set[str] = {
    "memo", "commit", "pr", "review-pr", "team-task",
}


@_require_auth
async def handle_unknown_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward unrecognized /commands to Claude as skill invocations.

    Claude agent has a Skill tool that can execute skills like /memo, /commit, etc.
    Bot-specific commands (/start, /kill, etc.) are handled by their own handlers
    and never reach here.

    CLI built-in commands (/skills, /help, etc.) are intercepted and either
    rewritten to natural language or rejected with a message.
    """

    text = update.message.text or ""
    # Extract command name (e.g. "/skills" -> "skills", "/skills@botname" -> "skills")
    cmd = text.split()[0].lstrip("/").split("@")[0].lower() if text else ""

    if cmd in CLI_BUILTIN_REWRITES:
        rewrite = CLI_BUILTIN_REWRITES[cmd]
        if rewrite is None:
            await update.message.reply_text(
                f"/{cmd} is a CLI-only command and not available via Telegram.",
            )
            return
        # Replace the original command text with the rewritten prompt
        update.message.text = rewrite
    elif cmd not in SKILL_COMMAND_ALLOWLIST:
        # Unknown command not in allowlist — reject locally, don't waste a Claude call
        await update.message.reply_text(
            f"Unknown command: /{cmd}\n"
            f"Use /start to see available commands.",
        )
        return

    # Forward to Claude (either original /skill command or rewritten prompt)
    await handle_message(update, ctx)


@_require_auth(is_command=False)
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular text messages - forward to Claude with per-session parallel execution."""
    topic_id = get_topic_id(update)

    text = update.message.text
    if not text:
        return

    # Strip @bot_username mention from message text (common in groups)
    text = strip_bot_mention(text)
    if not text:
        return

    # Check if this is a text answer to a pending AskUserQuestion "Other"
    if await _check_text_answer(update.message.chat_id, text):
        await update.message.reply_text("Answer received.")
        return

    # Group chat input guard: require @session prefix to trigger Claude call.
    # This prevents casual messages from wasting tokens.
    # Commands forwarded from handle_unknown_command (starting with "/") are exempt.
    if _is_group_chat(update) and not text.startswith("@") and not text.startswith("/"):
        return

    # Extract reply context (for cross-bot info transfer in groups)
    reply_context = extract_reply_context(update)

    prepared = run_prepare_message_use_case(
        topic_id=topic_id,
        text=text,
        reply_context=reply_context,
        resolve_session_target=resolve_session_target,
    )
    session_name = prepared.session_name
    session_id = prepared.session_id
    prompt = prepared.prompt
    lock = get_session_lock(session_id)

    # Busy session policy: do not queue new requests while one is running.
    if lock.locked():
        await update.message.reply_text(render_busy_reply(session_name))
        return

    # Acquire per-session lock - different sessions can run in parallel
    async with lock:

        # Guard: warn if a terminal process is actively using this SDK session.
        # Concurrent access to the same session from tgcc + terminal corrupts
        # the JSONL conversation history and leads to unpredictable results.
        sdk_sid = session_sdk_ids.get(session_id)
        if sdk_sid:
            active_pid = check_active_terminal_session(sdk_sid)
            if active_pid:
                await update.message.reply_text(
                    f"⚠️ Session `{session_name}` 的 SDK session 正在被终端进程 "
                    f"(PID {active_pid}) 使用中。\n"
                    f"同时从两边发消息会导致会话历史混乱。\n"
                    f"请先在终端中退出该 session，或使用 `/force` 前缀强制发送。",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

        # Send "typing" indicator
        await update.message.chat.send_action(
            ChatAction.TYPING,
            message_thread_id=_thread_id_or_none(topic_id),
        )

        # Send a "working on it" message with session label
        thinking_msg = await update.message.reply_text(
            render_progress_reply(session_name)
        )

        # Call Claude
        response, activity_log, usage_info = await call_claude(
            prompt, session_id,
            thinking_msg=thinking_msg, chat=update.message.chat,
            session_name=session_name,
            topic_id=topic_id,
        )

        # Finalize the activity log message with usage stats
        if thinking_msg:
            try:
                final_text = render_final_activity_reply(session_name, activity_log)
                usage_line = usage_info.summary_line()
                if usage_line:
                    final_text += f"\n📊 {usage_line}"
                await _tg_retry(lambda: thinking_msg.edit_text(final_text))
            except Exception:
                pass

        # Split and send response, prefixed with session name
        parts = run_label_response_parts_use_case(
            session_name, split_message(response)
        )
        for labeled in parts:
            try:
                await _tg_retry(lambda l=labeled: update.message.reply_text(
                    l, parse_mode=ParseMode.MARKDOWN))
            except Exception:
                await _tg_retry(lambda l=labeled: update.message.reply_text(l))

        # Auto-send files/images found in response
        sendable = extract_sendable_files(response)
        if sendable:
            await _tg_retry(lambda: send_files_to_chat(
                update.message.chat, sendable, session_name, topic_id))


# ─── Lifecycle callbacks ─────────────────────────────────────────────────────


async def _ssh_tunnel_health_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: restart SSH tunnel if it has died."""
    if _ssh_tunnel:
        try:
            _ssh_tunnel.ensure_alive()
        except Exception as e:
            log.error(f"SSH tunnel restart failed: {e}")


async def post_init(application: Application) -> None:
    """Post-init callback: set bot username."""
    global BOT_USERNAME
    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info(f"Bot username: @{BOT_USERNAME}")
    if OWNER_USER_ID:
        log.info(f"Owner user ID: {OWNER_USER_ID}")

    # Schedule SSH tunnel health check every 30 seconds
    if _ssh_tunnel and application.job_queue:
        application.job_queue.run_repeating(
            _ssh_tunnel_health_check, interval=30, first=10,
        )
        log.info("SSH tunnel health check scheduled (every 30s)")


async def post_shutdown(application: Application) -> None:
    """Post-shutdown callback."""
    _ = application
    flush = getattr(SESSION_REPO, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception as e:
            log.warning(f"Failed to flush session repository: {e}")


# ─── Main ────────────────────────────────────────────────────────────────────


def build_application() -> Application:
    """Build application and register handlers without starting polling."""
    from telegram.request import HTTPXRequest

    builder = (Application.builder()
               .token(BOT_TOKEN)
               .concurrent_updates(True)
               .post_init(post_init)
               .post_shutdown(post_shutdown))

    if _SOCKS_PROXY_URL:
        log.info(f"Using SOCKS5 proxy: {_SOCKS_PROXY_URL} (relay: {SSH_PROXY_RELAY})")
        proxy_request = HTTPXRequest(proxy=_SOCKS_PROXY_URL)
        builder = builder.request(proxy_request)
        # Also proxy the get_updates request used by the Updater
        builder = builder.get_updates_request(HTTPXRequest(proxy=_SOCKS_PROXY_URL))

    app = builder.build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("attach", cmd_attach))
    app.add_handler(CommandHandler("browse", cmd_browse))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("topic", cmd_topic))
    app.add_handler(CommandHandler("history", cmd_history))
    # AskUserQuestion callback handler (must be before general message handler)
    app.add_handler(CallbackQueryHandler(handle_ask_callback, pattern=r"^ask:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Catch-all: forward unrecognized /commands to Claude as skill invocations
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE,
        handle_file,
    ))
    return app


def main():
    log.info("Starting Claude Code Telegram Bot (Agent SDK)...")
    log.info(f"Working directory: {WORK_DIR}")
    log.info(f"Claude timeout: {CLAUDE_TIMEOUT}s")
    log.info(f"AskUser timeout: {ASK_USER_TIMEOUT}s")

    if SSH_PROXY_RELAY:
        log.info(f"SSH proxy relay: {SSH_PROXY_RELAY} (pid: {_ssh_tunnel.process.pid if _ssh_tunnel and _ssh_tunnel.process else 'N/A'})")

    if ALLOWED_USER_IDS:
        log.info(f"Allowed users: {ALLOWED_USER_IDS}")
    else:
        log.info("No user restriction - first /start user will be registered")

    app = build_application()

    log.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
