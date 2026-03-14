"""SDK session lookup utilities for /attach command.

Provides pure functions to:
- Find SDK session JSONL files and extract their cwd
- Check if a session is actively running in a terminal
- Validate SDK session ID format
"""

from __future__ import annotations

import json
import os
import re

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
