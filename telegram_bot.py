#!/usr/bin/env python3
"""
Telegram Bot for Claude Code remote interaction.
Allows controlling Claude Code via Telegram messages from your phone.

Uses Claude Agent SDK for structured communication with Claude Code,
including AskUserQuestion support via Telegram inline keyboards.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Support --instance flag to load named env file from instances/ directory
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--instance", type=str, default=None,
                     help="Instance name (loads instances/<name>.env)")
_args, _ = _parser.parse_known_args()

if _args.instance:
    _env_path = Path(__file__).parent / "instances" / f"{_args.instance}.env"
    if not _env_path.exists():
        print(f"Error: {_env_path} not found", file=sys.stderr)
        sys.exit(1)
    load_dotenv(_env_path, override=True)
else:
    load_dotenv(Path(__file__).parent / ".env", override=True)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode, ChatAction

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    ToolUseBlock,
    TextBlock,
    PermissionResultAllow,
    CLINotFoundError,
    ProcessError,
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
ALLOWED_USER_IDS: set[int] = set()
ALLOWED_USER_IDS_ENV = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
if ALLOWED_USER_IDS_ENV:
    ALLOWED_USER_IDS = {int(x.strip()) for x in ALLOWED_USER_IDS_ENV.split(",")}

# Working directory for claude CLI
WORK_DIR = os.environ.get("CLAUDE_WORK_DIR", os.getcwd())

# Claude CLI timeout (seconds) - 0 means no timeout, rely on --max-turns and /kill
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "0"))

# Heartbeat interval: send "still working" update every N seconds
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))

# Stall warning: warn user (not kill) if no new events for this many seconds
STALL_WARN_TIMEOUT = int(os.environ.get("STALL_WARN_TIMEOUT", "300"))

# Max agentic turns to prevent infinite loops
MAX_TURNS = int(os.environ.get("CLAUDE_MAX_TURNS", "150"))

# AskUserQuestion timeout (seconds) - how long to wait for user to answer
ASK_USER_TIMEOUT = int(os.environ.get("ASK_USER_TIMEOUT", "300"))

# Upload directory for files received from Telegram
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(WORK_DIR, "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Telegram message max length
TG_MAX_LEN = 4000

# Memory-notebook auto-sync script path
AUTO_SYNC_SCRIPT = os.path.expanduser(
    "~/.claude/skills/memory-notebook/scripts/auto-sync.sh"
)

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("claude-tg-bot")

# ─── Session & Lock management ──────────────────────────────────────────────

# Per-user current active session name: user_id -> session_name
user_active_session: dict[int, str] = {}

# Per-user all sessions: user_id -> {session_name: local_session_id}
user_all_sessions: dict[int, dict[str, str]] = {}

# local_session_id -> SDK session_id (set after first successful call)
session_sdk_ids: dict[str, str] = {}

# Per-SESSION asyncio lock: allows different sessions to run in parallel
session_locks: dict[str, asyncio.Lock] = {}

# Per-session pending message queue count
session_pending: dict[str, int] = {}

# Per-session running client: local_session_id -> ClaudeSDKClient
# Used by /kill command to interrupt stuck sessions
session_clients: dict[str, ClaudeSDKClient] = {}

# Auto-increment session counter per user for default naming
user_session_counter: dict[int, int] = {}

# Per-session working directory: local_session_id -> cwd path
session_work_dirs: dict[str, str] = {}

# Project shortname -> full path mapping
PROJECT_SHORTCUTS: dict[str, str] = {
    "tgcc": "/home/gkh/claude_tasks/claude-telegram-bot",
    "ashare": "/home/gkh/ashare",
    "casimir_ashare": "/home/gkh/ashare/casimir_ashare",
    "bookmodel": "/home/gkh/claude_tasks/bookmodel_slippage",
    "bookmodel_slippage": "/home/gkh/claude_tasks/bookmodel_slippage",
    "revenue": "/home/gkh/revenue",
    "rena": "/home/gkh/revenue",
    "tmp": "/home/gkh/claude_tasks/tmp_task",
}

# Pending AskUserQuestion futures: question_id -> asyncio.Future
pending_questions: dict[str, asyncio.Future] = {}
# question_id -> list of original options (for resolving callback index)
pending_question_options: dict[str, list[dict]] = {}


def get_session_lock(session_id: str) -> asyncio.Lock:
    """Get or create a lock for a session."""
    if session_id not in session_locks:
        session_locks[session_id] = asyncio.Lock()
    return session_locks[session_id]


def _next_default_name(user_id: int) -> str:
    """Generate next default session name like s1, s2, ..."""
    cnt = user_session_counter.get(user_id, 0) + 1
    user_session_counter[user_id] = cnt
    return f"s{cnt}"


def resolve_cwd(cwd_arg: Optional[str]) -> Optional[str]:
    """Resolve a cwd argument to an absolute path.

    Accepts: project shortname (e.g. 'casimir_ashare'), ~ path, absolute path,
    or relative path (resolved against global WORK_DIR).
    Returns absolute path if valid directory, None otherwise.
    """
    if not cwd_arg:
        return None

    # Check project shortcuts first
    if cwd_arg in PROJECT_SHORTCUTS:
        path = PROJECT_SHORTCUTS[cwd_arg]
    else:
        path = os.path.expanduser(cwd_arg)
        if not os.path.isabs(path):
            path = os.path.join(WORK_DIR, path)
        path = os.path.abspath(path)

    return path if os.path.isdir(path) else None


def get_session_cwd(session_id: str) -> str:
    """Get the working directory for a session (falls back to global WORK_DIR)."""
    return session_work_dirs.get(session_id, WORK_DIR)


def get_or_create_session(user_id: int) -> str:
    """Get existing session or create a new one. Returns local_session_id."""
    if user_id not in user_all_sessions:
        user_all_sessions[user_id] = {}

    active_name = user_active_session.get(user_id)
    if active_name and active_name in user_all_sessions[user_id]:
        return user_all_sessions[user_id][active_name]

    # No active session - create default
    name = _next_default_name(user_id)
    sid = str(uuid.uuid4())
    user_all_sessions[user_id][name] = sid
    user_active_session[user_id] = name
    log.info(f"New session for user {user_id}: {name} ({sid})")
    return sid


def create_new_session(user_id: int, name: Optional[str] = None,
                       cwd: Optional[str] = None) -> tuple[str, str]:
    """Create a new session and switch to it. Returns (name, local_session_id).

    Args:
        cwd: Optional per-session working directory (absolute path, already resolved).
             If None, inherits global WORK_DIR at call time.
    """
    if user_id not in user_all_sessions:
        user_all_sessions[user_id] = {}

    if not name:
        name = _next_default_name(user_id)

    # If name already exists, generate a unique one
    if name in user_all_sessions[user_id]:
        base = name
        i = 2
        while f"{base}{i}" in user_all_sessions[user_id]:
            i += 1
        name = f"{base}{i}"

    sid = str(uuid.uuid4())
    user_all_sessions[user_id][name] = sid
    user_active_session[user_id] = name

    # Store per-session cwd (if provided, otherwise get_session_cwd falls back to WORK_DIR)
    if cwd:
        session_work_dirs[sid] = cwd

    log.info(f"Created session for user {user_id}: {name} ({sid})"
             f"{f' cwd={cwd}' if cwd else ''}")
    return name, sid


def switch_session(user_id: int, name: str) -> Optional[str]:
    """Switch to an existing session by name. Returns session_id or None if not found."""
    sessions = user_all_sessions.get(user_id, {})
    if name not in sessions:
        return None
    user_active_session[user_id] = name
    log.info(f"User {user_id} switched to session: {name}")
    return sessions[name]


def clear_session(user_id: int, target_name: str | None = None) -> tuple[str, str, str | None]:
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
    sessions = user_all_sessions.get(user_id, {})

    if target_name and target_name in sessions:
        # Clear a specific named session: keep name and cwd, reset context
        old_sid = sessions[target_name]
        old_cwd = session_work_dirs.get(old_sid)
        session_sdk_ids.pop(old_sid, None)
        session_work_dirs.pop(old_sid, None)
        session_locks.pop(old_sid, None)
        session_pending.pop(old_sid, None)
        session_clients.pop(old_sid, None)

        new_sid = str(uuid.uuid4())
        sessions[target_name] = new_sid
        if old_cwd:
            session_work_dirs[new_sid] = old_cwd
        log.info(f"Session '{target_name}' cleared for user {user_id}, "
                 f"new sid: {new_sid} (kept name and cwd={old_cwd})")
        return target_name, new_sid, old_cwd

    # Clear active session (original behavior): destroy and create new auto-named
    old_name = user_active_session.get(user_id)
    old_cwd = None
    if old_name and old_name in sessions:
        old_sid = sessions.pop(old_name)
        old_cwd = session_work_dirs.get(old_sid)
        session_sdk_ids.pop(old_sid, None)
        session_work_dirs.pop(old_sid, None)
        session_locks.pop(old_sid, None)
        session_pending.pop(old_sid, None)
        session_clients.pop(old_sid, None)

    name = _next_default_name(user_id)
    sid = str(uuid.uuid4())
    if user_id not in user_all_sessions:
        user_all_sessions[user_id] = {}
    user_all_sessions[user_id][name] = sid
    user_active_session[user_id] = name
    log.info(f"Session cleared for user {user_id}, new: {name} ({sid})")
    return name, sid, old_cwd


def delete_session(user_id: int, name: str) -> tuple[bool, str, str | None]:
    """Delete a specific session by name. Returns (success, message, deleted_cwd)."""
    sessions = user_all_sessions.get(user_id, {})
    if name not in sessions:
        return False, f"Session `{name}` not found.", None

    sid = sessions[name]

    # Refuse to delete a busy session
    lock = session_locks.get(sid)
    if lock and lock.locked():
        return False, f"Session `{name}` is busy. Use /kill first.", None

    # Capture cwd before cleanup
    deleted_cwd = get_session_cwd(sid)

    # Clean up all data structures
    sessions.pop(name)
    session_sdk_ids.pop(sid, None)
    session_work_dirs.pop(sid, None)
    session_locks.pop(sid, None)
    session_pending.pop(sid, None)
    session_clients.pop(sid, None)

    # If deleted the active session, switch to another or create new
    if user_active_session.get(user_id) == name:
        if sessions:
            new_active = next(iter(sessions))
            user_active_session[user_id] = new_active
        else:
            new_name = _next_default_name(user_id)
            new_sid = str(uuid.uuid4())
            sessions[new_name] = new_sid
            user_active_session[user_id] = new_name

    log.info(f"Deleted session '{name}' ({sid[:8]}) for user {user_id}")
    return True, f"Session `{name}` deleted.", deleted_cwd


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


def list_sessions(user_id: int) -> list[tuple[str, str, bool, bool]]:
    """List all sessions for a user. Returns [(name, session_id, is_active, is_busy), ...]."""
    sessions = user_all_sessions.get(user_id, {})
    active = user_active_session.get(user_id)
    result = []
    for name, sid in sessions.items():
        lock = session_locks.get(sid)
        is_busy = lock.locked() if lock else False
        result.append((name, sid, name == active, is_busy))
    return result


def resolve_session_target(user_id: int, text: str) -> tuple[str, str, str]:
    """Parse @session_name prefix from message text.

    Returns (session_name, local_session_id, remaining_prompt).
    If no @prefix, uses active session.
    If @name doesn't exist, auto-creates it (inherits active session's cwd).
    """
    if text.startswith("@") and " " in text:
        target_name, prompt = text.split(" ", 1)
        target_name = target_name[1:]  # strip @

        sessions = user_all_sessions.get(user_id, {})
        if target_name in sessions:
            sid = sessions[target_name]
            return target_name, sid, prompt
        else:
            # Auto-create new session without switching active
            # Inherit cwd from active session
            prev_active = user_active_session.get(user_id)
            inherit_cwd = None
            if prev_active and prev_active in sessions:
                active_sid = sessions[prev_active]
                inherit_cwd = session_work_dirs.get(active_sid)
            name, sid = create_new_session(user_id, target_name, cwd=inherit_cwd)
            if prev_active:
                user_active_session[user_id] = prev_active
            log.info(f"Auto-created session '{name}' for @mention routing"
                     f"{f' (inherited cwd={inherit_cwd})' if inherit_cwd else ''}")
            return name, sid, prompt

    # Default: use active session
    sid = get_or_create_session(user_id)
    active_name = user_active_session.get(user_id, "?")
    return active_name, sid, text


# ─── Auth check ──────────────────────────────────────────────────────────────


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # No restriction if not configured
    return user_id in ALLOWED_USER_IDS


# ─── Activity extraction from SDK messages ──────────────────────────────────

# Tool name → user-friendly description
TOOL_LABELS = {
    "Read": "Reading",
    "Edit": "Editing",
    "Write": "Writing",
    "Bash": "Running command",
    "Grep": "Searching",
    "Glob": "Finding files",
    "Task": "Running sub-agent",
    "Agent": "Running sub-agent",
    "WebFetch": "Fetching web page",
    "WebSearch": "Searching web",
    "TodoWrite": "Updating tasks",
    "AskUserQuestion": "Asking user",
}


def _extract_activity(msg) -> Optional[str]:
    """Extract user-friendly activity description from an SDK message."""
    if not isinstance(msg, AssistantMessage):
        return None

    for block in msg.content:
        if isinstance(block, ToolUseBlock):
            tool_name = block.name
            label = TOOL_LABELS.get(tool_name, tool_name)
            inp = block.input or {}
            if tool_name in ("Read", "Edit", "Write") and "file_path" in inp:
                path = inp["file_path"]
                short = path.split("/")[-1]  # just filename
                return f"{label} {short}"
            elif tool_name == "Bash" and "command" in inp:
                cmd = inp["command"][:40]
                return f"{label}: {cmd}"
            elif tool_name == "Grep" and "pattern" in inp:
                return f"{label} '{inp['pattern'][:30]}'"
            elif tool_name == "Glob" and "pattern" in inp:
                return f"{label} {inp['pattern'][:30]}"
            elif tool_name in ("Task", "Agent"):
                desc = inp.get("description", "")[:30]
                return f"{label}: {desc}" if desc else label
            return label
        elif isinstance(block, TextBlock):
            text = block.text
            if text:
                return "Thinking..."

    return None


async def _update_thinking_msg(thinking_msg, activity_log: list[str],
                               session_name: str, elapsed: float):
    """Update the thinking message with current activity log (throttled by caller)."""
    if not thinking_msg:
        return
    mins, secs = divmod(int(elapsed), 60)
    time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
    label = f"[{session_name}] " if session_name else ""
    recent = activity_log[-10:]
    log_text = "\n".join(f"  ▸ {a}" for a in recent)
    if len(activity_log) > 10:
        log_text = f"  ... ({len(activity_log) - 10} earlier)\n" + log_text
    try:
        await thinking_msg.edit_text(f"{label}Working... ({time_str})\n{log_text}")
    except Exception:
        pass


# ─── AskUserQuestion handling ───────────────────────────────────────────────


def _make_can_use_tool(chat, session_name: str):
    """Create a can_use_tool callback that forwards AskUserQuestion to Telegram."""

    async def can_use_tool(tool_name, tool_input, context):
        if tool_name == "AskUserQuestion":
            return await _handle_ask_user_question(tool_input, chat, session_name)
        # Allow all other tools
        return PermissionResultAllow(updated_input=tool_input)

    return can_use_tool


async def _handle_ask_user_question(tool_input: dict, chat, session_name: str):
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
            await chat.send_message(msg_text, reply_markup=markup)
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
                    f"{label}No answer in {ASK_USER_TIMEOUT}s, auto-selected: {fallback}")
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
    if not query or not query.data or not query.data.startswith("ask:"):
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    _, qid, choice = parts
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

    # Regular option selection
    try:
        option_idx = int(choice)
        options = pending_question_options.get(qid, [])
        if 0 <= option_idx < len(options):
            selected = options[option_idx]["label"]
        else:
            selected = choice
    except ValueError:
        selected = choice

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


async def call_claude(prompt: str, session_id: str,
                      thinking_msg=None, chat=None,
                      session_name: str = "") -> tuple[str, list[str]]:
    """Call Claude via Agent SDK with real-time activity tracking.

    Uses ClaudeSDKClient for streaming mode (required for can_use_tool callback).
    Session persistence via resume=sdk_session_id.

    Returns: (response_text, activity_log) tuple
    """
    # Build options (use per-session cwd if set, otherwise global WORK_DIR)
    effective_cwd = get_session_cwd(session_id)
    options = ClaudeAgentOptions(
        cwd=effective_cwd,
        max_turns=MAX_TURNS,
        permission_mode="bypassPermissions",
        can_use_tool=_make_can_use_tool(chat, session_name) if chat else None,
        setting_sources=["user", "project"],
    )

    # If we have a saved SDK session_id, resume it
    sdk_sid = session_sdk_ids.get(session_id)
    if sdk_sid:
        options.resume = sdk_sid

    log.info(f"Calling Claude SDK for session {session_id[:8]}... "
             f"(resume={'yes' if sdk_sid else 'no'}, cwd={effective_cwd})")

    client = ClaudeSDKClient(options=options)
    session_clients[session_id] = client

    try:
        await client.connect()
        await client.query(prompt)

        activity_log: list[str] = []
        result_text = ""
        actual_session_id = None
        start_time = time.time()
        last_edit_time = 0.0
        last_heartbeat_time = start_time
        EDIT_THROTTLE = 3.0

        async for msg in client.receive_response():
            now = time.time()
            elapsed = now - start_time

            # Total timeout check
            if CLAUDE_TIMEOUT > 0 and elapsed >= CLAUDE_TIMEOUT:
                log.error(f"Claude SDK timed out after {int(elapsed)}s")
                try:
                    await client.interrupt()
                except Exception:
                    pass
                return (f"[Timeout] Total timeout ({CLAUDE_TIMEOUT}s) exceeded. "
                        f"Use /kill to stop earlier."), activity_log

            # Extract activity
            activity = _extract_activity(msg)
            if activity and (not activity_log or activity_log[-1] != activity):
                activity_log.append(activity)

            # Throttled update of thinking_msg
            if thinking_msg and activity and (now - last_edit_time) >= EDIT_THROTTLE:
                last_edit_time = now
                await _update_thinking_msg(thinking_msg, activity_log,
                                           session_name, elapsed)

            # Heartbeat: send typing action periodically
            if chat and (now - last_heartbeat_time) >= HEARTBEAT_INTERVAL:
                last_heartbeat_time = now
                try:
                    await chat.send_action(ChatAction.TYPING)
                except Exception:
                    pass

                # Stall warning
                if not activity_log or (now - last_edit_time) >= STALL_WARN_TIMEOUT:
                    if thinking_msg:
                        mins, secs = divmod(int(elapsed), 60)
                        time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                        label = f"[{session_name}] " if session_name else ""
                        last_act = activity_log[-1] if activity_log else "Starting..."
                        try:
                            await thinking_msg.edit_text(
                                f"{label}Long thinking... ({time_str})\n"
                                f"Last: {last_act}\n"
                                f"Use /kill {session_name} if stuck")
                        except Exception:
                            pass

            # Capture result
            if isinstance(msg, ResultMessage):
                actual_session_id = msg.session_id
                result_text = msg.result or ""
                if msg.is_error:
                    result_text = f"[Error] {result_text}"
                if msg.subtype == "error_max_turns":
                    result_text += (f"\n\n[Reached max turns ({msg.num_turns}). "
                                    f"Task may be incomplete.]")

        # Save SDK session_id for future resume
        if actual_session_id:
            session_sdk_ids[session_id] = actual_session_id

        return result_text or "[Task completed but no summary was produced.]", activity_log

    except CLINotFoundError:
        return "Error: claude CLI not found. Make sure it's installed.", []
    except ProcessError as e:
        error_msg = str(e)
        # Handle "already in use" by retrying with resume
        if "already in use" in error_msg and not sdk_sid:
            log.warning(f"Session in use, retrying with resume for {session_id[:8]}")
            # Try to find the SDK session_id from error context
            # For now, just report the error
            return f"Error: Session is already in use. Try /clear to reset.", []
        return f"Error: {error_msg}", []
    except Exception as e:
        log.exception("Unexpected error calling Claude SDK")
        return f"Error {type(e).__name__}: {e}", []
    finally:
        session_clients.pop(session_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass


# ─── Message splitting ───────────────────────────────────────────────────────


def split_message(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    """Split long messages for Telegram's 4096 char limit."""
    if len(text) <= max_len:
        return [text]

    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break

        # Try to split at newline
        split_pos = text.rfind("\n", 0, max_len)
        if split_pos == -1:
            # Try space
            split_pos = text.rfind(" ", 0, max_len)
        if split_pos == -1:
            split_pos = max_len

        parts.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    return parts


# ─── File auto-send ──────────────────────────────────────────────────────────

# Image extensions that can be sent as photos (Telegram supports these natively)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# File extensions worth auto-sending (common output formats)
SENDABLE_EXTS = IMAGE_EXTS | {
    ".pdf", ".csv", ".xlsx", ".xls", ".docx", ".doc",
    ".html", ".svg", ".mp4", ".mp3", ".zip", ".tar", ".gz",
    ".txt", ".md", ".json",
}

# Telegram file size limit (50MB for bots)
TG_FILE_SIZE_LIMIT = 50 * 1024 * 1024

# Regex to find file paths in response text
# Matches absolute paths and paths starting with ./
_FILE_PATH_RE = re.compile(
    r'(?:^|[\s`\'"])(/[\w./_-]+\.[\w]+|\.\/[\w./_-]+\.[\w]+)',
)


def extract_sendable_files(text: str) -> list[tuple[str, bool]]:
    """Extract file paths from response text that exist on disk and are worth sending.

    Returns list of (path, is_image) tuples, deduplicated and ordered by appearance.
    """
    seen = set()
    files = []

    for match in _FILE_PATH_RE.finditer(text):
        path = match.group(1)
        # Resolve relative paths against WORK_DIR
        if path.startswith("./"):
            path = os.path.join(WORK_DIR, path[2:])
        path = os.path.abspath(path)

        if path in seen:
            continue
        seen.add(path)

        if not os.path.isfile(path):
            continue

        ext = os.path.splitext(path)[1].lower()
        if ext not in SENDABLE_EXTS:
            continue

        # Check file size
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size == 0 or size > TG_FILE_SIZE_LIMIT:
            continue

        is_image = ext in IMAGE_EXTS
        files.append((path, is_image))

    return files


async def send_files_to_chat(chat, files: list[tuple[str, bool]], session_name: str = ""):
    """Send extracted files to Telegram chat as photos or documents."""
    from telegram import InputFile

    label = f"[{session_name}] " if session_name else ""

    for path, is_image in files:
        filename = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                if is_image:
                    await chat.send_photo(
                        photo=InputFile(f, filename=filename),
                        caption=f"{label}{filename}",
                    )
                else:
                    await chat.send_document(
                        document=InputFile(f, filename=filename),
                        caption=f"{label}{filename}",
                    )
            log.info(f"Sent file to chat: {path}")
        except Exception as e:
            log.warning(f"Failed to send file {path}: {e}")


# ─── Telegram handlers ───────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    user_id = user.id

    # Auto-register first user if no restriction set
    if not ALLOWED_USER_IDS:
        ALLOWED_USER_IDS.add(user_id)
        log.info(f"Auto-registered user: {user.full_name} (ID: {user_id})")

    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized.")
        return

    session_id = get_or_create_session(user_id)
    active_name = user_active_session.get(user_id, "?")
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
        f"/cd [path|shortname] - Change session cwd\n"
        f"/session - Current session info\n"
        f"/kill [name] - Kill stuck session\n"
        f"/sync - Sync memory notebook\n\n"
        f"Send any message to interact with Claude.\n"
        f"Use `@name msg` to send to a specific session.\n"
        f"Different sessions run in parallel!\n\n"
        f"Per-session cwd: use `/new build casimir_ashare`\n"
        f"to create a session bound to a project.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear [name] - reset a session's conversation context.

    Usage:
        /clear           - clear active session (destroys it, creates new auto-named)
        /clear casimir   - clear named session (keeps name and cwd, resets context)
    """
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    target_name = ctx.args[0] if ctx.args else None

    # Validate target exists
    if target_name:
        sessions = user_all_sessions.get(user_id, {})
        if target_name not in sessions:
            available = ", ".join(f"`{n}`" for n in sorted(sessions.keys()))
            await update.message.reply_text(
                f"Session `{target_name}` not found.\n"
                f"Available: {available or 'none'}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    cleared_name, new_sid, old_cwd = clear_session(user_id, target_name)

    if target_name:
        # Named clear: kept name, reset context
        home = os.path.expanduser("~")
        cwd_display = get_session_cwd(new_sid).replace(home, "~")
        reply = (
            f"Session `{cleared_name}` cleared (context reset).\n"
            f"📁 cwd: `{cwd_display}` (preserved)"
        )
    else:
        # Active session clear (original behavior)
        reply = (
            f"Conversation cleared.\n"
            f"New session: `{cleared_name}` (`{new_sid[:8]}...`)"
        )

    msg = await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # Auto-sync memory notebook from the old session's cwd
    if old_cwd:
        sync_status = await run_auto_sync(old_cwd)
        if sync_status == "synced":
            try:
                await msg.edit_text(
                    reply + "\n📓 Memory notebook synced.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete <name> - delete a specific session."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/delete <session_name>`\n"
            "Use /sessions to see all sessions.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    target_name = ctx.args[0]
    success, message, deleted_cwd = delete_session(user_id, target_name)

    if not success:
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        return

    # Show new active session info
    active_name = user_active_session.get(user_id, "?")
    remaining = len(user_all_sessions.get(user_id, {}))
    reply = f"{message}\nActive: `{active_name}` | Total: {remaining}"
    msg = await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # Auto-sync memory notebook from the deleted session's cwd
    if deleted_cwd:
        sync_status = await run_auto_sync(deleted_cwd)
        if sync_status == "synced":
            try:
                await msg.edit_text(
                    reply + "\n📓 Memory notebook synced.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass


async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sync - manually trigger memory notebook sync."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    # Use active session's cwd for project detection
    active_name = user_active_session.get(user_id)
    sessions = user_all_sessions.get(user_id, {})
    if active_name and active_name in sessions:
        sid = sessions[active_name]
        cwd = get_session_cwd(sid)
    else:
        cwd = WORK_DIR

    home = os.path.expanduser("~")
    display_cwd = cwd.replace(home, "~")
    msg = await update.message.reply_text(
        f"📓 Syncing memory notebook...\n(project: `{display_cwd}`)",
        parse_mode=ParseMode.MARKDOWN,
    )

    sync_status = await run_auto_sync(cwd)

    status_map = {
        "synced": "📓 Memory notebook synced.",
        "sync script not found": "❌ Sync script not found.",
    }
    status_text = status_map.get(sync_status, f"⚠️ Sync: {sync_status}")

    try:
        await msg.edit_text(
            f"{status_text}\n(project: `{display_cwd}`)",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        await update.message.reply_text(status_text)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new [name] [cwd] - create a new session and switch to it.

    Usage:
        /new                     - auto-named, uses global WORK_DIR
        /new build               - named 'build', uses global WORK_DIR
        /new build casimir_ashare - named 'build', cwd = project shortname
        /new build ~/ashare      - named 'build', cwd = expanded path
    """
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    name = None
    cwd = None

    if ctx.args:
        name = ctx.args[0]
        if len(ctx.args) >= 2:
            # Second arg onwards is cwd (join in case path has spaces, though unlikely)
            cwd_arg = " ".join(ctx.args[1:])
            resolved = resolve_cwd(cwd_arg)
            if resolved:
                cwd = resolved
            else:
                # Check if it looks like a shortname typo
                available = ", ".join(f"`{k}`" for k in sorted(PROJECT_SHORTCUTS.keys()))
                await update.message.reply_text(
                    f"⚠️ Directory not found: `{cwd_arg}`\n\n"
                    f"Available shortcuts: {available}\n"
                    f"Or use an absolute/relative path.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

    new_name, new_sid = create_new_session(user_id, name, cwd=cwd)
    effective = get_session_cwd(new_sid)
    # Show shortened path for display
    display_cwd = effective.replace(os.path.expanduser("~"), "~")
    cwd_note = ""
    if cwd:
        cwd_arg = " ".join(ctx.args[1:])
        cwd_note = " (shortcut)" if cwd_arg in PROJECT_SHORTCUTS else ""
    await update.message.reply_text(
        f"✅ New session: `{new_name}` (`{new_sid[:8]}...`)\n"
        f"📁 cwd: `{display_cwd}`{cwd_note}\n\n"
        f"Use /sessions to see all sessions.\n"
        f"Use /switch <name> to switch back.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /switch <name> - switch to an existing session."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not ctx.args:
        # Show list and hint
        sessions = list_sessions(user_id)
        if not sessions:
            await update.message.reply_text("No sessions. Send a message to create one.")
            return
        lines = ["Sessions:"]
        for name, sid, is_active, is_busy in sessions:
            marker = " (active)" if is_active else ""
            busy = " [BUSY]" if is_busy else ""
            lines.append(f"  `{name}` - `{sid[:8]}...`{marker}{busy}")
        lines.append("\nUsage: /switch <name>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    target = ctx.args[0]
    sid = switch_session(user_id, target)
    if sid:
        await update.message.reply_text(
            f"Switched to session: `{target}` (`{sid[:8]}...`)",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Try fuzzy match
        sessions = user_all_sessions.get(user_id, {})
        matches = [n for n in sessions if target.lower() in n.lower()]
        if matches:
            hint = ", ".join(f"`{m}`" for m in matches)
            await update.message.reply_text(
                f"Session `{target}` not found. Did you mean: {hint}?",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"Session `{target}` not found. Use /sessions to see all.",
                parse_mode=ParseMode.MARKDOWN,
            )


async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sessions - list all sessions."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    sessions = list_sessions(user_id)
    if not sessions:
        await update.message.reply_text("No sessions yet. Send a message to start one.")
        return

    home = os.path.expanduser("~")
    lines = ["All sessions:"]
    for name, sid, is_active, is_busy in sessions:
        marker = " <- active" if is_active else ""
        has_context = sid in session_sdk_ids
        status = "BUSY" if is_busy else ("has context" if has_context else "empty")
        s_cwd = get_session_cwd(sid).replace(home, "~")
        lines.append(f"  `{name}` ({status}){marker}\n    📁 `{s_cwd}`")
    lines.append(f"\nTotal: {len(sessions)}")
    lines.append(
        "\nQuick reference:\n"
        "  /switch <name> - Switch to session\n"
        "  /new [name] [cwd] - Create new session\n"
        "  /delete <name> - Delete a session\n"
        "  /clear [name] - Reset session context\n"
        "  /cd [path] - Change session cwd\n"
        "  /kill [name] - Kill busy session\n"
        "  /sync - Sync memory notebook"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status - show bot status."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    active_name = user_active_session.get(user_id, "none")
    sessions = user_all_sessions.get(user_id, {})
    busy_sessions = []
    for name, sid in sessions.items():
        lock = session_locks.get(sid)
        if lock and lock.locked():
            busy_sessions.append(name)

    home = os.path.expanduser("~")
    active_sid = sessions.get(active_name)
    active_cwd = get_session_cwd(active_sid).replace(home, "~") if active_sid else "N/A"
    global_cwd = WORK_DIR.replace(home, "~")
    await update.message.reply_text(
        f"Bot Status:\n"
        f"- Running: yes\n"
        f"- User ID: `{user_id}`\n"
        f"- Active session: `{active_name}`\n"
        f"- Session cwd: `{active_cwd}`\n"
        f"- Global default cwd: `{global_cwd}`\n"
        f"- Total sessions: {len(sessions)}\n"
        f"- Busy sessions: {', '.join(f'`{n}`' for n in busy_sessions) if busy_sessions else 'none'}\n"
        f"- Timeout: {CLAUDE_TIMEOUT}s\n"
        f"- Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cd [path|shortname] - change working directory for current session.

    Usage:
        /cd                      - show current session's cwd
        /cd casimir_ashare       - change to project shortname
        /cd ~/ashare             - change to expanded path
        /cd --global <path>      - change global default WORK_DIR (affects new sessions)
    """
    global WORK_DIR
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    # Get current active session
    active_name = user_active_session.get(user_id)
    sessions = user_all_sessions.get(user_id, {})
    active_sid = sessions.get(active_name) if active_name else None
    home = os.path.expanduser("~")

    if not ctx.args:
        if active_sid:
            s_cwd = get_session_cwd(active_sid).replace(home, "~")
            is_custom = active_sid in session_work_dirs
            global_cwd = WORK_DIR.replace(home, "~")
            msg = f"Session `{active_name}` cwd: `{s_cwd}`"
            if is_custom:
                msg += f"\nGlobal default: `{global_cwd}`"
        else:
            msg = f"Global cwd: `{WORK_DIR.replace(home, '~')}`"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # Check --global flag
    args = list(ctx.args)
    is_global = False
    if args[0] == "--global":
        is_global = True
        args = args[1:]
        if not args:
            await update.message.reply_text(
                f"Global default: `{WORK_DIR.replace(home, '~')}`\n"
                f"Usage: /cd --global <path|shortname>",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    cwd_arg = " ".join(args)
    resolved = resolve_cwd(cwd_arg)

    if not resolved:
        available = ", ".join(f"`{k}`" for k in sorted(PROJECT_SHORTCUTS.keys()))
        await update.message.reply_text(
            f"Directory not found: `{cwd_arg}`\n\n"
            f"Available shortcuts: {available}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    display = resolved.replace(home, "~")
    shortcut_note = " (shortcut)" if cwd_arg in PROJECT_SHORTCUTS else ""

    if is_global:
        WORK_DIR = resolved
        await update.message.reply_text(
            f"Global default changed to:\n`{display}`{shortcut_note}\n"
            f"(Affects new sessions without custom cwd)",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        if not active_sid:
            await update.message.reply_text("No active session. Send a message to create one first.")
            return
        session_work_dirs[active_sid] = resolved
        await update.message.reply_text(
            f"Session `{active_name}` cwd changed to:\n`{display}`{shortcut_note}",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /session - show current session info."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    active_name = user_active_session.get(user_id)
    sessions = user_all_sessions.get(user_id, {})
    if active_name and active_name in sessions:
        sid = sessions[active_name]
        has_context = sid in session_sdk_ids
        status = "has context" if has_context else "empty"
        home = os.path.expanduser("~")
        s_cwd = get_session_cwd(sid).replace(home, "~")
        is_custom = sid in session_work_dirs
        cwd_label = f"`{s_cwd}`" + (" (custom)" if is_custom else " (global)")
        await update.message.reply_text(
            f"Current session: `{active_name}` ({status})\n"
            f"Session ID: `{sid}`\n"
            f"Work dir: {cwd_label}\n"
            f"Total sessions: {len(sessions)}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("No active session. Send a message to start one.")


async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /kill [session_name] - interrupt a running Claude session."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    sessions = user_all_sessions.get(user_id, {})
    if not sessions:
        await update.message.reply_text("No sessions.")
        return

    # Determine target session
    if ctx.args:
        target_name = ctx.args[0]
    else:
        # Default: kill active session
        target_name = user_active_session.get(user_id)

    if not target_name or target_name not in sessions:
        # Show busy sessions as hint
        busy = []
        for name, sid in sessions.items():
            if sid in session_clients:
                busy.append(name)
        if busy:
            await update.message.reply_text(
                f"Session `{target_name}` not found.\nBusy sessions: {', '.join(f'`{n}`' for n in busy)}\n"
                f"Usage: /kill <session_name>",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text("No busy sessions to kill.")
        return

    sid = sessions[target_name]
    client = session_clients.get(sid)
    if not client:
        await update.message.reply_text(f"Session `{target_name}` is not running.", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        await client.interrupt()
        log.info(f"User interrupted session {target_name} ({sid[:8]})")
        await update.message.reply_text(
            f"Interrupted session `{target_name}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to interrupt: {e}")


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file/photo/video uploads - download to server and optionally forward to Claude."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized. Send /start first.")
        return

    msg = update.message
    file_obj = None
    original_name = None

    # Priority: document > photo > video > audio > voice > video_note
    if msg.document:
        file_obj = msg.document
        original_name = msg.document.file_name or f"doc_{int(datetime.now().timestamp())}"
    elif msg.photo:
        # photo is a list of sizes, take the largest
        file_obj = msg.photo[-1]
        original_name = f"photo_{int(datetime.now().timestamp())}.jpg"
    elif msg.video:
        file_obj = msg.video
        original_name = msg.video.file_name or f"video_{int(datetime.now().timestamp())}.mp4"
    elif msg.audio:
        file_obj = msg.audio
        original_name = msg.audio.file_name or f"audio_{int(datetime.now().timestamp())}.mp3"
    elif msg.voice:
        file_obj = msg.voice
        original_name = f"voice_{int(datetime.now().timestamp())}.ogg"
    elif msg.video_note:
        file_obj = msg.video_note
        original_name = f"videonote_{int(datetime.now().timestamp())}.mp4"

    if not file_obj:
        await msg.reply_text("Unsupported file type.")
        return

    # Download file
    try:
        tg_file = await file_obj.get_file()
        save_path = os.path.join(UPLOAD_DIR, original_name)

        # Avoid overwriting: add suffix if exists
        if os.path.exists(save_path):
            base, ext = os.path.splitext(original_name)
            save_path = os.path.join(UPLOAD_DIR, f"{base}_{int(datetime.now().timestamp())}{ext}")

        await tg_file.download_to_drive(save_path)
        log.info(f"File downloaded: {save_path} (from user {user_id})")
    except Exception as e:
        log.exception("File download failed")
        await msg.reply_text(f"Download failed: {e}")
        return

    # If there's a caption, forward file path + caption to Claude as a prompt
    caption = msg.caption or ""
    if caption:
        # Resolve session from caption FIRST (before prepending file path)
        session_name, session_id, resolved_caption = resolve_session_target(user_id, caption)
        prompt_text = f"File saved to: {save_path}\n\n{resolved_caption}"
        lock = get_session_lock(session_id)

        session_pending[session_id] = session_pending.get(session_id, 0) + 1
        if lock.locked():
            queue_pos = session_pending.get(session_id, 1)
            await msg.reply_text(f"[{session_name}] File received, queued (position {queue_pos})...")

        async with lock:
            session_pending[session_id] = max(0, session_pending.get(session_id, 1) - 1)

            await msg.chat.send_action(ChatAction.TYPING)
            thinking_msg = await msg.reply_text(f"[{session_name}] Processing file + message...")

            response, activity_log = await call_claude(
                prompt_text, session_id,
                thinking_msg=thinking_msg, chat=msg.chat,
                session_name=session_name,
            )

            # Finalize the activity log message (keep it, don't delete)
            if thinking_msg:
                label = f"[{session_name}] " if session_name else ""
                if activity_log:
                    log_text = "\n".join(f"  ▸ {a}" for a in activity_log)
                    final_log = f"{label}Done ({len(activity_log)} steps)\n{log_text}"
                else:
                    final_log = f"{label}Done"
                try:
                    await thinking_msg.edit_text(final_log)
                except Exception:
                    pass

            parts = split_message(response)
            for i, part in enumerate(parts):
                labeled = f"[{session_name}] {part}" if i == 0 else part
                try:
                    await msg.reply_text(labeled, parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    await msg.reply_text(labeled)

            # Auto-send files/images found in response
            sendable = extract_sendable_files(response)
            if sendable:
                await send_files_to_chat(msg.chat, sendable, session_name)
    else:
        # No caption - just confirm the file was saved
        await msg.reply_text(
            f"File saved to:\n`{save_path}`\n\n"
            f"Send a message referencing this path to ask Claude about it.",
            parse_mode=ParseMode.MARKDOWN,
        )


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

    # Forward to Claude (either original /skill command or rewritten prompt)
    await handle_message(update, ctx)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular text messages - forward to Claude with per-session parallel execution."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized. Send /start first.")
        return

    text = update.message.text
    if not text:
        return

    # Check if this is a text answer to a pending AskUserQuestion "Other"
    if await _check_text_answer(update.message.chat_id, text):
        await update.message.reply_text("Answer received.")
        return

    # Resolve target session: supports @session_name prefix
    session_name, session_id, prompt = resolve_session_target(user_id, text)
    lock = get_session_lock(session_id)

    # Track pending count per session
    session_pending[session_id] = session_pending.get(session_id, 0) + 1

    # If this session's lock is held, tell user their message is queued
    if lock.locked():
        queue_pos = session_pending.get(session_id, 1)
        await update.message.reply_text(
            f"[{session_name}] Queued (position {queue_pos}). Processing previous message...",
        )

    # Acquire per-session lock - different sessions can run in parallel
    async with lock:
        session_pending[session_id] = max(0, session_pending.get(session_id, 1) - 1)

        # Send "typing" indicator
        await update.message.chat.send_action(ChatAction.TYPING)

        # Send a "working on it" message with session label
        thinking_msg = await update.message.reply_text(f"[{session_name}] Thinking...")

        # Call Claude
        response, activity_log = await call_claude(
            prompt, session_id,
            thinking_msg=thinking_msg, chat=update.message.chat,
            session_name=session_name,
        )

        # Finalize the activity log message (keep it, don't delete)
        if thinking_msg:
            label = f"[{session_name}] " if session_name else ""
            if activity_log:
                log_text = "\n".join(f"  ▸ {a}" for a in activity_log)
                final_log = f"{label}Done ({len(activity_log)} steps)\n{log_text}"
            else:
                final_log = f"{label}Done"
            try:
                await thinking_msg.edit_text(final_log)
            except Exception:
                pass

        # Split and send response, prefixed with session name
        parts = split_message(response)
        for i, part in enumerate(parts):
            labeled = f"[{session_name}] {part}" if i == 0 else part
            try:
                await update.message.reply_text(labeled, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(labeled)

        # Auto-send files/images found in response
        sendable = extract_sendable_files(response)
        if sendable:
            await send_files_to_chat(update.message.chat, sendable, session_name)


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    log.info("Starting Claude Code Telegram Bot (Agent SDK)...")
    log.info(f"Working directory: {WORK_DIR}")
    log.info(f"Claude timeout: {CLAUDE_TIMEOUT}s")
    log.info(f"AskUser timeout: {ASK_USER_TIMEOUT}s")

    if ALLOWED_USER_IDS:
        log.info(f"Allowed users: {ALLOWED_USER_IDS}")
    else:
        log.info("No user restriction - first /start user will be registered")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("sync", cmd_sync))
    # AskUserQuestion callback handler (must be before general message handler)
    app.add_handler(CallbackQueryHandler(handle_ask_callback, pattern=r"^ask:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Catch-all: forward unrecognized /commands to Claude as skill invocations
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE,
        handle_file,
    ))

    log.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
