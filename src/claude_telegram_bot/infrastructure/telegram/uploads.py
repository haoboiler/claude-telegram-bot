from __future__ import annotations

import os
from datetime import datetime
from typing import Optional


def _ts(now_ts: Optional[int] = None) -> int:
    return int(now_ts) if now_ts is not None else int(datetime.now().timestamp())


def select_upload_file(msg, now_ts: Optional[int] = None):
    """Pick upload file object and original name using existing priority rules."""
    if msg.document:
        file_obj = msg.document
        original_name = msg.document.file_name or f"doc_{_ts(now_ts)}"
        return file_obj, original_name
    if msg.photo:
        file_obj = msg.photo[-1]
        original_name = f"photo_{_ts(now_ts)}.jpg"
        return file_obj, original_name
    if msg.video:
        file_obj = msg.video
        original_name = msg.video.file_name or f"video_{_ts(now_ts)}.mp4"
        return file_obj, original_name
    if msg.audio:
        file_obj = msg.audio
        original_name = msg.audio.file_name or f"audio_{_ts(now_ts)}.mp3"
        return file_obj, original_name
    if msg.voice:
        file_obj = msg.voice
        original_name = f"voice_{_ts(now_ts)}.ogg"
        return file_obj, original_name
    if msg.video_note:
        file_obj = msg.video_note
        original_name = f"videonote_{_ts(now_ts)}.mp4"
        return file_obj, original_name
    return None, None


def resolve_upload_path(
    upload_dir: str,
    original_name: str,
    *,
    exists_fn=os.path.exists,
    now_ts: Optional[int] = None,
) -> str:
    """Resolve save path and add timestamp suffix if target already exists."""
    save_path = os.path.join(upload_dir, original_name)
    if exists_fn(save_path):
        base, ext = os.path.splitext(original_name)
        save_path = os.path.join(upload_dir, f"{base}_{_ts(now_ts)}{ext}")
    return save_path
