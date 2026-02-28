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


def create_new_session(user_id: int, name: Optional[str] = None) -> tuple[str, str]:
    """Create a new session and switch to it. Returns (name, local_session_id)."""
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
    log.info(f"Created session for user {user_id}: {name} ({sid})")
    return name, sid


def switch_session(user_id: int, name: str) -> Optional[str]:
    """Switch to an existing session by name. Returns session_id or None if not found."""
    sessions = user_all_sessions.get(user_id, {})
    if name not in sessions:
        return None
    user_active_session[user_id] = name
    log.info(f"User {user_id} switched to session: {name}")
    return sessions[name]


def clear_session(user_id: int) -> str:
    """Clear current session and create a new one (replaces current slot)."""
    old_name = user_active_session.get(user_id)
    if old_name and user_id in user_all_sessions:
        old_sid = user_all_sessions[user_id].pop(old_name, None)
        if old_sid:
            session_sdk_ids.pop(old_sid, None)

    name = _next_default_name(user_id)
    sid = str(uuid.uuid4())
    if user_id not in user_all_sessions:
        user_all_sessions[user_id] = {}
    user_all_sessions[user_id][name] = sid
    user_active_session[user_id] = name
    log.info(f"Session cleared for user {user_id}, new: {name} ({sid})")
    return sid


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
    If @name doesn't exist, auto-creates it.
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
            prev_active = user_active_session.get(user_id)
            name, sid = create_new_session(user_id, target_name)
            if prev_active:
                user_active_session[user_id] = prev_active
            log.info(f"Auto-created session '{name}' for @mention routing")
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
    # Build options
    options = ClaudeAgentOptions(
        cwd=WORK_DIR,
        max_turns=MAX_TURNS,
        permission_mode="bypassPermissions",
        can_use_tool=_make_can_use_tool(chat, session_name) if chat else None,
    )

    # If we have a saved SDK session_id, resume it
    sdk_sid = session_sdk_ids.get(session_id)
    if sdk_sid:
        options.resume = sdk_sid

    log.info(f"Calling Claude SDK for session {session_id[:8]}... "
             f"(resume={'yes' if sdk_sid else 'no'})")

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
        f"/new [name] - New session (keeps old)\n"
        f"/switch <name> - Switch session\n"
        f"/sessions - List all sessions\n"
        f"/clear - Reset current session\n"
        f"/status - Bot status\n"
        f"/cd <path> - Change working dir\n"
        f"/session - Current session info\n"
        f"/kill [name] - Kill stuck session\n\n"
        f"Send any message to interact with Claude.\n"
        f"Use `@name msg` to send to a specific session.\n"
        f"Different sessions run in parallel!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear - reset current conversation (destroys current session, creates new one)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    new_session = clear_session(user_id)
    active_name = user_active_session.get(user_id, "?")
    await update.message.reply_text(
        f"Conversation cleared.\n"
        f"New session: `{active_name}` (`{new_session[:8]}...`)",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new [name] - create a new session and switch to it (old session preserved)."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    name = " ".join(ctx.args) if ctx.args else None
    new_name, new_sid = create_new_session(user_id, name)
    await update.message.reply_text(
        f"New session created: `{new_name}` (`{new_sid[:8]}...`)\n"
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

    lines = ["All sessions:"]
    for name, sid, is_active, is_busy in sessions:
        marker = " <- active" if is_active else ""
        has_context = sid in session_sdk_ids
        status = "BUSY" if is_busy else ("has context" if has_context else "empty")
        lines.append(f"  `{name}` ({status}){marker}")
    lines.append(f"\nTotal: {len(sessions)}")
    lines.append("Use /switch <name> to switch, /new [name] to create.")
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

    await update.message.reply_text(
        f"Bot Status:\n"
        f"- Running: yes\n"
        f"- User ID: `{user_id}`\n"
        f"- Active session: `{active_name}`\n"
        f"- Total sessions: {len(sessions)}\n"
        f"- Busy sessions: {', '.join(f'`{n}`' for n in busy_sessions) if busy_sessions else 'none'}\n"
        f"- Work dir: `{WORK_DIR}`\n"
        f"- Timeout: {CLAUDE_TIMEOUT}s\n"
        f"- Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cd - change working directory."""
    global WORK_DIR
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    if not ctx.args:
        await update.message.reply_text(f"Current: `{WORK_DIR}`", parse_mode=ParseMode.MARKDOWN)
        return

    new_dir = " ".join(ctx.args)
    new_dir = os.path.expanduser(new_dir)
    if not os.path.isabs(new_dir):
        new_dir = os.path.join(WORK_DIR, new_dir)
    new_dir = os.path.abspath(new_dir)

    if os.path.isdir(new_dir):
        WORK_DIR = new_dir
        await update.message.reply_text(
            f"Working directory changed to:\n`{WORK_DIR}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(f"Directory not found: `{new_dir}`", parse_mode=ParseMode.MARKDOWN)


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
        await update.message.reply_text(
            f"Current session: `{active_name}` ({status})\n"
            f"Session ID: `{sid}`\n"
            f"Work dir: `{WORK_DIR}`\n"
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
    # AskUserQuestion callback handler (must be before general message handler)
    app.add_handler(CallbackQueryHandler(handle_ask_callback, pattern=r"^ask:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE,
        handle_file,
    ))

    log.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
