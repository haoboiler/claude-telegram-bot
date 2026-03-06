from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class FileCaptionPlan:
    forward_to_claude: bool
    reply_text: str | None
    parse_mode: str | None
    session_name: str | None = None
    session_id: str | None = None
    prompt_text: str | None = None


def run_prepare_file_caption_use_case(
    *,
    topic_id: int,
    caption: str,
    save_path: str,
    resolve_session_target: Callable[[int, str], tuple[str, str, str]],
) -> FileCaptionPlan:
    """Prepare file-caption forwarding plan without Telegram side effects."""
    if not caption:
        return FileCaptionPlan(
            forward_to_claude=False,
            reply_text=(
                f"File saved to:\n`{save_path}`\n\n"
                f"Send a message referencing this path to ask Claude about it."
            ),
            parse_mode="Markdown",
        )

    session_name, session_id, resolved_caption = resolve_session_target(topic_id, caption)
    return FileCaptionPlan(
        forward_to_claude=True,
        reply_text=None,
        parse_mode=None,
        session_name=session_name,
        session_id=session_id,
        prompt_text=f"File saved to: {save_path}\n\n{resolved_caption}",
    )
