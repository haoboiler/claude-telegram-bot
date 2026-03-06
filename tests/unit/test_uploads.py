"""Tests for infrastructure/telegram/uploads.py."""

from __future__ import annotations

import pytest

from src.claude_telegram_bot.infrastructure.telegram.uploads import (
    resolve_upload_path,
    select_upload_file,
)


class _Obj:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestSelectUploadFile:
    def test_document_priority(self):
        msg = _Obj(
            document=_Obj(file_name="a.txt"),
            photo=[_Obj(id="small"), _Obj(id="big")],
            video=None,
            audio=None,
            voice=None,
            video_note=None,
        )
        file_obj, original_name = select_upload_file(msg, now_ts=123)
        assert original_name == "a.txt"
        assert file_obj.file_name == "a.txt"

    def test_photo_fallback(self):
        msg = _Obj(
            document=None,
            photo=[_Obj(id="small"), _Obj(id="big")],
            video=None,
            audio=None,
            voice=None,
            video_note=None,
        )
        file_obj, original_name = select_upload_file(msg, now_ts=100)
        assert file_obj is not None
        assert "100" in original_name or "photo" in original_name.lower()


class TestResolveUploadPath:
    def test_no_conflict(self):
        path = resolve_upload_path(
            "/tmp", "a.txt", exists_fn=lambda _p: False, now_ts=999
        )
        assert path == "/tmp/a.txt"

    def test_conflict_adds_timestamp(self):
        path = resolve_upload_path(
            "/tmp", "a.txt", exists_fn=lambda _p: True, now_ts=999
        )
        assert path == "/tmp/a_999.txt"
