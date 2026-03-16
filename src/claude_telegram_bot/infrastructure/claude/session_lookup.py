"""SDK session lookup utilities for /attach and /browse commands.

Provides pure functions to:
- Find SDK session JSONL files and extract their cwd
- Check if a session is actively running in a terminal
- Validate SDK session ID format
- Browse / list SDK sessions across project directories
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_CLAUDE_DIR = os.path.expanduser("~/.claude")


def validate_sdk_session_id(value: str) -> bool:
    """Check whether *value* looks like a valid UUID v4 session ID."""
    return bool(_UUID_RE.match(value))


def find_sdk_session(sdk_session_id: str) -> tuple[str, str] | None:
    """Locate the JSONL file for *sdk_session_id* and extract its cwd.

    Searches ``~/.claude/projects/*/`` for ``<sdk_session_id>.jsonl``,
    then reads the first ``progress`` event that carries a ``"cwd"`` field.

    Returns
    -------
    tuple[str, str] | None
        ``(jsonl_path, cwd)`` on success, ``None`` if not found or cwd
        cannot be determined.
    """
    projects_dir = os.path.join(_CLAUDE_DIR, "projects")
    if not os.path.isdir(projects_dir):
        return None

    target_fname = f"{sdk_session_id}.jsonl"

    for dirname in os.listdir(projects_dir):
        candidate = os.path.join(projects_dir, dirname, target_fname)
        if os.path.isfile(candidate):
            cwd = _extract_cwd_from_jsonl(candidate)
            if cwd:
                return candidate, cwd
    return None


def check_active_terminal_session(sdk_session_id: str) -> int | None:
    """Return the PID if *sdk_session_id* is actively running in a terminal.

    Reads ``~/.claude/sessions/*.json`` files, matches ``sessionId``,
    and verifies the PID is still alive.

    Returns ``None`` when the session is not active.
    """
    sessions_dir = os.path.join(_CLAUDE_DIR, "sessions")
    if not os.path.isdir(sessions_dir):
        return None

    for fname in os.listdir(sessions_dir):
        fpath = os.path.join(sessions_dir, fname)
        try:
            with open(fpath) as fh:
                data = json.loads(fh.read())
        except (json.JSONDecodeError, OSError):
            continue

        if data.get("sessionId") != sdk_session_id:
            continue

        pid = data.get("pid")
        if pid and _pid_alive(int(pid)):
            return int(pid)

    return None


# ── internal helpers ──────────────────────────────────────────────────────


def _extract_cwd_from_jsonl(jsonl_path: str) -> str | None:
    """Read the first ``"cwd"`` value from a session JSONL file.

    Only scans the first 20 lines to avoid reading large files.
    """
    try:
        with open(jsonl_path) as fh:
            for i, line in enumerate(fh):
                if i >= 20:
                    break
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = data.get("cwd")
                if cwd and isinstance(cwd, str):
                    return cwd
    except OSError:
        pass
    return None


def _pid_alive(pid: int) -> bool:
    """Return *True* if a process with *pid* exists."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _extract_first_message(jsonl_path: str) -> str:
    """Extract the first user message from a session JSONL file.

    Scans the first 30 lines for a user message or queue-operation with
    displayable text content.  Returns an empty string on failure.
    """
    try:
        with open(jsonl_path) as fh:
            for i, line in enumerate(fh):
                if i >= 30:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # User message with content
                if d.get("type") == "user":
                    msg = d.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str) and content:
                        return content[:60]
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text = c.get("text", "")
                                if text:
                                    return text[:60]
                # Queue enqueue with display text
                if (
                    d.get("type") == "queue-operation"
                    and d.get("operation") == "enqueue"
                ):
                    c = d.get("content", {})
                    if isinstance(c, dict):
                        text = c.get("display", "") or c.get("text", "")
                        if text:
                            return text[:60]
    except OSError:
        pass
    return ""


# ── Browse / list SDK sessions ──────────────────────────────────────────


@dataclass
class SdkSessionInfo:
    """Metadata for a single SDK session JSONL file."""

    session_id: str
    project_dir: str  # encoded directory name under ~/.claude/projects/
    cwd: str
    size_bytes: int
    mtime: float
    first_message: str


def list_project_dirs() -> list[tuple[str, int]]:
    """Return ``(dirname, session_count)`` for each project directory.

    Sorted alphabetically.
    """
    projects_dir = os.path.join(_CLAUDE_DIR, "projects")
    if not os.path.isdir(projects_dir):
        return []

    result: list[tuple[str, int]] = []
    for dirname in sorted(os.listdir(projects_dir)):
        full = os.path.join(projects_dir, dirname)
        if not os.path.isdir(full):
            continue
        count = sum(1 for f in os.listdir(full) if f.endswith(".jsonl"))
        if count > 0:
            result.append((dirname, count))
    return result


def list_sdk_sessions(
    project_filter: str | None = None,
    max_age_days: int = 7,
) -> list[SdkSessionInfo]:
    """Scan ``~/.claude/projects/`` for SDK session JSONL files.

    Parameters
    ----------
    project_filter
        If given, only scan directories whose name contains this
        substring (case-insensitive).  Use ``None`` to scan all.
    max_age_days
        Only return sessions modified within this many days.

    Returns
    -------
    list[SdkSessionInfo]
        Sessions sorted by modification time (newest first).
    """
    projects_dir = os.path.join(_CLAUDE_DIR, "projects")
    if not os.path.isdir(projects_dir):
        return []

    cutoff = time.time() - max_age_days * 86400
    sessions: list[SdkSessionInfo] = []

    for dirname in os.listdir(projects_dir):
        full = os.path.join(projects_dir, dirname)
        if not os.path.isdir(full):
            continue
        if project_filter and project_filter.lower() not in dirname.lower():
            continue

        for fname in os.listdir(full):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(full, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue

            sid = fname[:-6]  # strip .jsonl
            if not _UUID_RE.match(sid):
                continue

            cwd = _extract_cwd_from_jsonl(fpath) or "?"
            first_msg = _extract_first_message(fpath)

            sessions.append(
                SdkSessionInfo(
                    session_id=sid,
                    project_dir=dirname,
                    cwd=cwd,
                    size_bytes=st.st_size,
                    mtime=st.st_mtime,
                    first_message=first_msg,
                )
            )

    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions
