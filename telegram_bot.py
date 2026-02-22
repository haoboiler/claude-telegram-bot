#!/usr/bin/env python3
"""
Telegram Bot for Claude Code remote interaction.
Allows controlling Claude Code via Telegram messages from your phone.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode, ChatAction

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

# Claude CLI timeout (seconds) - 30 min default, complex tasks can take long
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))

# Heartbeat interval: send "still working" update every N seconds
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))

# Stall warning: warn user (not kill) if no new events for this many seconds
STALL_WARN_TIMEOUT = int(os.environ.get("STALL_WARN_TIMEOUT", "300"))

# Max agentic turns to prevent infinite loops
MAX_TURNS = int(os.environ.get("CLAUDE_MAX_TURNS", "30"))

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

# Per-user all sessions: user_id -> {session_name: session_id}
user_all_sessions: dict[int, dict[str, str]] = {}

# Track whether a session has been used (first msg uses --session-id, subsequent use --resume)
# Key: session_id (not user_id), so each session tracks its own init state
session_initialized: dict[str, bool] = {}

# Per-SESSION asyncio lock: allows different sessions to run in parallel
session_locks: dict[str, asyncio.Lock] = {}

# Per-session pending message queue count
session_pending: dict[str, int] = {}

# Per-session running process: session_id -> asyncio.subprocess.Process
# Used by /kill command to terminate stuck sessions
session_processes: dict[str, asyncio.subprocess.Process] = {}

# Auto-increment session counter per user for default naming
user_session_counter: dict[int, int] = {}


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


def get_or_create_session(user_id: int) -> tuple[str, bool]:
    """Get existing session or create a new one. Returns (session_id, is_new)."""
    if user_id not in user_all_sessions:
        user_all_sessions[user_id] = {}

    active_name = user_active_session.get(user_id)
    if active_name and active_name in user_all_sessions[user_id]:
        sid = user_all_sessions[user_id][active_name]
        is_new = not session_initialized.get(sid, False)
        return sid, is_new

    # No active session - create default
    name = _next_default_name(user_id)
    sid = str(uuid.uuid4())
    user_all_sessions[user_id][name] = sid
    user_active_session[user_id] = name
    session_initialized[sid] = False
    log.info(f"New session for user {user_id}: {name} ({sid})")
    return sid, True


def mark_session_initialized_by_sid(sid: str):
    """Mark a session as initialized after first successful call."""
    session_initialized[sid] = True


def create_new_session(user_id: int, name: Optional[str] = None) -> tuple[str, str]:
    """Create a new session and switch to it. Returns (name, session_id)."""
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
    session_initialized[sid] = False
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
        # Remove old session
        old_sid = user_all_sessions[user_id].pop(old_name, None)
        if old_sid:
            session_initialized.pop(old_sid, None)

    name = _next_default_name(user_id)
    sid = str(uuid.uuid4())
    if user_id not in user_all_sessions:
        user_all_sessions[user_id] = {}
    user_all_sessions[user_id][name] = sid
    user_active_session[user_id] = name
    session_initialized[sid] = False
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


def resolve_session_target(user_id: int, text: str) -> tuple[str, str, str, bool]:
    """Parse @session_name prefix from message text.

    Returns (session_name, session_id, remaining_prompt, is_new).
    If no @prefix, uses active session.
    If @name doesn't exist, auto-creates it.
    """
    if text.startswith("@") and " " in text:
        target_name, prompt = text.split(" ", 1)
        target_name = target_name[1:]  # strip @

        sessions = user_all_sessions.get(user_id, {})
        if target_name in sessions:
            # Existing session
            sid = sessions[target_name]
            is_new = not session_initialized.get(sid, False)
            return target_name, sid, prompt, is_new
        else:
            # Auto-create new session without switching active
            prev_active = user_active_session.get(user_id)
            name, sid = create_new_session(user_id, target_name)
            if prev_active:
                user_active_session[user_id] = prev_active
            log.info(f"Auto-created session '{name}' for @mention routing")
            return name, sid, prompt, True

    # Default: use active session
    sid, is_new = get_or_create_session(user_id)
    active_name = user_active_session.get(user_id, "?")
    return active_name, sid, text, is_new


# ─── Auth check ──────────────────────────────────────────────────────────────


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # No restriction if not configured
    return user_id in ALLOWED_USER_IDS


# ─── Stream-JSON activity parsing ────────────────────────────────────────────

# Tool name → user-friendly description
TOOL_LABELS = {
    "Read": "Reading",
    "Edit": "Editing",
    "Write": "Writing",
    "Bash": "Running command",
    "Grep": "Searching",
    "Glob": "Finding files",
    "Task": "Running sub-agent",
    "WebFetch": "Fetching web page",
    "WebSearch": "Searching web",
    "TodoWrite": "Updating tasks",
}


def _format_activity(event: dict) -> Optional[str]:
    """Extract user-friendly activity description from a stream-json event."""
    if event.get("type") == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                label = TOOL_LABELS.get(tool_name, tool_name)
                # Extract short context from input
                inp = block.get("input", {})
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
                elif tool_name == "Task":
                    desc = inp.get("description", "")[:30]
                    return f"{label}: {desc}" if desc else label
                return label
            elif block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    return "Thinking..."
    return None


# ─── Claude CLI interaction ──────────────────────────────────────────────────


async def call_claude(prompt: str, session_id: str, is_new: bool = True,
                      thinking_msg=None, chat=None, session_name: str = "") -> str:
    """Call claude CLI with stream-json output for real-time activity tracking.

    is_new: True = first message (use --session-id to create),
            False = subsequent (use --resume to continue)
    thinking_msg: the "Thinking..." message to update with activity
    chat: the chat object for sending typing actions
    """
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--max-turns", str(MAX_TURNS),
    ]

    if is_new:
        cmd += ["--session-id", session_id]
    else:
        cmd += ["--resume", session_id]

    log.info(f"Calling claude with session {session_id[:8]}... (is_new={is_new})")

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORK_DIR,
            env=env,
        )

        # Register process for /kill command
        session_processes[session_id] = proc

        start_time = asyncio.get_event_loop().time()
        last_event_time = start_time
        current_activity = "Starting..."
        received_events = False  # Track if Claude started processing
        stall_warned = False  # Only warn once per stall period
        final_result = None
        result_subtype = None
        result_errors = []
        result_num_turns = 0
        # Track text per-turn: list of (turn_index, text) pairs
        # Only the LAST assistant turn's text is the final response
        last_assistant_text_parts = []
        current_turn_text = []

        try:
            while True:
                now = asyncio.get_event_loop().time()
                elapsed = int(now - start_time)
                since_last_event = int(now - last_event_time)

                # Total timeout - the ONLY auto-kill mechanism
                if elapsed >= CLAUDE_TIMEOUT:
                    log.error(f"Claude CLI timed out after {elapsed}s")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    if received_events and is_new:
                        mark_session_initialized_by_sid(session_id)
                    return f"[Timeout] Total timeout ({CLAUDE_TIMEOUT}s) exceeded. Use /kill to stop earlier."

                # Stall warning - warn user but do NOT kill
                # (Long API calls produce no events, killing them is wrong)
                if since_last_event >= STALL_WARN_TIMEOUT and not stall_warned:
                    stall_warned = True
                    log.warning(f"No events for {since_last_event}s, session {session_id[:8]} may be in long API call")
                    if thinking_msg:
                        mins, secs = divmod(elapsed, 60)
                        time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                        label = f"[{session_name}] " if session_name else ""
                        try:
                            await thinking_msg.edit_text(
                                f"{label}Long thinking... no events for {since_last_event}s ({time_str})\n"
                                f"Last: {current_activity}\n"
                                f"Use /kill {session_name} if stuck"
                            )
                        except Exception:
                            pass

            # Try to read a line with heartbeat interval timeout
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=HEARTBEAT_INTERVAL
                )
            except asyncio.TimeoutError:
                # No new line - send heartbeat with current activity
                if thinking_msg:
                    mins, secs = divmod(elapsed, 60)
                    time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                    label = f"[{session_name}] " if session_name else ""
                    try:
                        await thinking_msg.edit_text(
                            f"{label}{current_activity} ({time_str})"
                        )
                    except Exception:
                        pass
                if chat:
                    try:
                        await chat.send_action(ChatAction.TYPING)
                    except Exception:
                        pass
                continue

            if not line:
                # EOF - process ended
                break

            # Parse the JSON event
            last_event_time = asyncio.get_event_loop().time()
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue

            received_events = True
            stall_warned = False  # Reset: events are flowing again

            # Extract activity for display
            activity = _format_activity(event)
            if activity:
                current_activity = activity

            # Capture result
            if event.get("type") == "result":
                final_result = event.get("result", "")
                result_subtype = event.get("subtype", "")
                result_errors = event.get("errors", [])
                result_num_turns = event.get("num_turns", 0)
            elif event.get("type") == "assistant":
                has_text = False
                has_tool = False
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            current_turn_text.append(text)
                            has_text = True
                    elif block.get("type") == "tool_use":
                        has_tool = True
                # When we see a tool_use, the current text is intermediate narration.
                # When we see text-only (no tool), it's likely a final response.
                # Save and reset per-turn tracking on each assistant message.
                if current_turn_text:
                    if has_tool:
                        # Text before tool calls = intermediate narration, discard for final output
                        current_turn_text = []
                    else:
                        # Text-only assistant message = likely final response
                        last_assistant_text_parts = current_turn_text[:]
                        current_turn_text = []
            elif event.get("type") == "user":
                # New user turn = reset, any subsequent assistant text is fresh
                current_turn_text = []

        finally:
            # Clean up process tracking
            session_processes.pop(session_id, None)

            # Wait for process to finish
            try:
                await proc.wait()
            except Exception:
                pass

        # Mark session as initialized if we received any events
        if received_events and is_new:
            mark_session_initialized_by_sid(session_id)

        # --- Result extraction priority ---
        # 1. result event's result field (best case: Claude provided final text)
        if final_result:
            return final_result

        # 2. Handle error subtypes from result event
        if result_subtype and result_subtype != "success":
            if result_subtype == "error_max_turns":
                summary = "\n".join(last_assistant_text_parts) if last_assistant_text_parts else ""
                warning = f"\n\n[Reached max turns ({result_num_turns}). Task may be incomplete.]"
                return (summary + warning) if summary else f"[Reached max turns ({result_num_turns}). No summary produced.]"
            elif result_errors:
                return f"[Error] {'; '.join(result_errors)}"

        # 3. Last text-only assistant message (the actual conversational response)
        if last_assistant_text_parts:
            return "\n".join(last_assistant_text_parts)

        # 4. If we have leftover current_turn_text (text that preceded the final tool call)
        if current_turn_text:
            return "\n".join(current_turn_text)

        # 5. Fallback: read any remaining stderr
        stderr = await proc.stderr.read()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and err:
            # Auto-retry: if "already in use" and we tried --session-id, retry with --resume
            if is_new and "already in use" in err:
                log.warning(f"Session {session_id[:8]} already exists on disk, retrying with --resume")
                mark_session_initialized_by_sid(session_id)
                return await call_claude(prompt, session_id, is_new=False,
                                         thinking_msg=thinking_msg, chat=chat,
                                         session_name=session_name)
            return f"Error Claude CLI failed (code {proc.returncode}):\n{err}"

        return "[Task completed but no summary was produced by Claude.]"

    except FileNotFoundError:
        return "Error claude CLI not found. Make sure it's in PATH."
    except Exception as e:
        log.exception("Unexpected error calling claude")
        return f"Error {type(e).__name__}: {e}"


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

    session_id, _ = get_or_create_session(user_id)
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
        initialized = session_initialized.get(sid, False)
        status = "BUSY" if is_busy else ("has context" if initialized else "empty")
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
        initialized = session_initialized.get(sid, False)
        status = "has context" if initialized else "empty"
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
    """Handle /kill [session_name] - kill a running Claude process."""
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
            if sid in session_processes:
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
    proc = session_processes.get(sid)
    if not proc:
        await update.message.reply_text(f"Session `{target_name}` is not running.", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        proc.kill()
        log.info(f"User killed session {target_name} ({sid[:8]})")
        await update.message.reply_text(
            f"Killed session `{target_name}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to kill: {e}")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular text messages - forward to Claude with per-session parallel execution."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized. Send /start first.")
        return

    text = update.message.text
    if not text:
        return

    # Resolve target session: supports @session_name prefix
    session_name, session_id, prompt, is_new = resolve_session_target(user_id, text)
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

        # Re-check is_new inside lock (may have changed while queued)
        is_new = not session_initialized.get(session_id, False)

        # Send "typing" indicator
        await update.message.chat.send_action(ChatAction.TYPING)

        # Send a "working on it" message with session label
        thinking_msg = await update.message.reply_text(f"[{session_name}] Thinking...")

        # Call Claude: first msg creates session, subsequent msgs resume it
        response = await call_claude(
            prompt, session_id, is_new=is_new,
            thinking_msg=thinking_msg, chat=update.message.chat,
            session_name=session_name,
        )

        # Note: session initialization is now handled inside call_claude itself
        # (marked as initialized once any events are received from Claude CLI)

        # Delete the "thinking" message
        try:
            await thinking_msg.delete()
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


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    log.info("Starting Claude Code Telegram Bot...")
    log.info(f"Working directory: {WORK_DIR}")
    log.info(f"Claude timeout: {CLAUDE_TIMEOUT}s")

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
