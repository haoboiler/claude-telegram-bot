from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class PreparedMessage:
    session_name: str
    session_id: str
    prompt: str


def run_prepare_message_use_case(
    *,
    topic_id: int,
    text: str,
    reply_context: str | None,
    resolve_session_target: Callable[[int, str], tuple[str, str, str]],
) -> PreparedMessage:
    """Resolve target session and final prompt text for a user message."""
    session_name, session_id, prompt = resolve_session_target(topic_id, text)
    if reply_context:
        prompt = f"{reply_context}\n\nUser's message: {prompt}"
    return PreparedMessage(
        session_name=session_name,
        session_id=session_id,
        prompt=prompt,
    )


def render_busy_reply(session_name: str, *, is_file_caption: bool = False) -> str:
    """Render busy-session cancellation text."""
    if is_file_caption:
        return (
            f"[{session_name}] Busy processing previous request. "
            f"This file+caption message was canceled."
        )
    return (
        f"[{session_name}] Busy processing previous request. "
        f"This message was canceled."
    )


def render_progress_reply(session_name: str, *, is_file_caption: bool = False) -> str:
    """Render in-progress status text."""
    if is_file_caption:
        return f"[{session_name}] Processing file + message..."
    return f"[{session_name}] Thinking..."


def render_final_activity_reply(
    session_name: str, activity_log: list[str], max_len: int = 3500
) -> str:
    """Render final cumulative activity log text.

    Keeps the full step count in the header but trims older lines from the
    front if the rendered log would exceed ``max_len`` (Telegram messages cap
    at 4096 chars), so the edit never silently fails on long sessions.
    """
    label = f"[{session_name}] " if session_name else ""
    if not activity_log:
        return f"{label}Done"

    header = f"{label}Done ({len(activity_log)} steps)\n"
    lines = [f"  ▸ {a}" for a in activity_log]
    log_text = "\n".join(lines)

    # Trim oldest lines until it fits, prepending an elision marker.
    dropped = 0
    while lines and len(header) + len(log_text) > max_len:
        lines.pop(0)
        dropped += 1
        log_text = "\n".join(lines)
    if dropped:
        log_text = f"  ... ({dropped} earlier)\n{log_text}"

    return f"{header}{log_text}"


def run_label_response_parts_use_case(session_name: str, parts: list[str]) -> list[str]:
    """Prefix first response chunk with session label, preserving existing behavior."""
    labeled_parts: list[str] = []
    for i, part in enumerate(parts):
        labeled_parts.append(f"[{session_name}] {part}" if i == 0 else part)
    return labeled_parts
