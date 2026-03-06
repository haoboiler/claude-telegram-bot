"""Tests for message_flow and file_flow use cases."""

from __future__ import annotations

import pytest

from src.claude_telegram_bot.application.use_cases.message_flow import (
    render_busy_reply,
    render_final_activity_reply,
    render_progress_reply,
    run_label_response_parts_use_case,
    run_prepare_message_use_case,
)
from src.claude_telegram_bot.application.use_cases.file_flow import (
    run_prepare_file_caption_use_case,
)


class TestPrepareMessage:
    def test_basic_routing(self):
        result = run_prepare_message_use_case(
            topic_id=1,
            text="hello",
            reply_context="ctx",
            resolve_session_target=lambda _tid, raw: (
                "main",
                "sid-main",
                f"prompt:{raw}",
            ),
        )
        assert result.session_name == "main"
        assert result.session_id == "sid-main"
        assert result.prompt == "ctx\n\nUser's message: prompt:hello"


class TestRenderHelpers:
    def test_busy_reply(self):
        assert (
            render_busy_reply("main")
            == "[main] Busy processing previous request. This message was canceled."
        )

    def test_busy_reply_file_caption(self):
        assert (
            render_busy_reply("main", is_file_caption=True)
            == "[main] Busy processing previous request. This file+caption message was canceled."
        )

    def test_progress_reply(self):
        assert render_progress_reply("main") == "[main] Thinking..."

    def test_progress_reply_file(self):
        assert (
            render_progress_reply("main", is_file_caption=True)
            == "[main] Processing file + message..."
        )

    def test_final_activity_reply(self):
        assert (
            render_final_activity_reply("main", ["read", "write"])
            == "[main] Done (2 steps)\n  \u25b8 read\n  \u25b8 write"
        )

    def test_label_response_parts(self):
        assert run_label_response_parts_use_case("main", ["first", "second"]) == [
            "[main] first",
            "second",
        ]


class TestFileFlow:
    def test_no_caption_no_forward(self):
        result = run_prepare_file_caption_use_case(
            topic_id=1,
            caption="",
            save_path="/tmp/a.txt",
            resolve_session_target=lambda _tid, _txt: (
                "main",
                "sid-main",
                "prompt",
            ),
        )
        assert result.forward_to_claude is False
        assert "File saved to" in (result.reply_text or "")
        assert result.parse_mode == "Markdown"

    def test_caption_forwards_to_claude(self):
        result = run_prepare_file_caption_use_case(
            topic_id=1,
            caption="analyze this file",
            save_path="/tmp/a.txt",
            resolve_session_target=lambda _tid, txt: (
                "main",
                "sid-main",
                f"resolved:{txt}",
            ),
        )
        assert result.forward_to_claude is True
        assert result.session_name == "main"
        assert result.session_id == "sid-main"
        assert (
            result.prompt_text
            == "File saved to: /tmp/a.txt\n\nresolved:analyze this file"
        )
