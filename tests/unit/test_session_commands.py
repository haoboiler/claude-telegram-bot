"""Tests for application/use_cases/session_commands.py."""

from __future__ import annotations

import pytest

from src.claude_telegram_bot.application.use_cases.session_commands import (
    run_cd_use_case,
    run_clear_session_use_case,
    run_delete_session_use_case,
    run_history_resolve_use_case,
    run_kill_use_case,
    run_new_session_use_case,
    run_session_info_use_case,
    run_sessions_use_case,
    run_status_use_case,
    run_switch_session_use_case,
    run_sync_finish_use_case,
    run_sync_init_use_case,
    run_topic_use_case,
)


class TestSwitchSession:
    def test_empty_sessions(self):
        result = run_switch_session_use_case(
            topic_id=0,
            args=[],
            list_sessions=lambda _tid: [],
            switch_session=lambda _tid, _name: None,
            topic_all_sessions={},
        )
        assert result.reply_text == "No sessions. Send a message to create one."

    def test_no_args_shows_usage(self):
        result = run_switch_session_use_case(
            topic_id=0,
            args=[],
            list_sessions=lambda _tid: [("main", "sid", True, False)],
            switch_session=lambda _tid, _name: None,
            topic_all_sessions={0: {"main": "sid"}},
        )
        assert "Usage" in result.reply_text or "main" in result.reply_text


class TestNewSession:
    def test_invalid_cwd(self):
        result = run_new_session_use_case(
            topic_id=0,
            args=["build", "missing_dir"],
            resolve_cwd=lambda _arg: None,
            get_project_shortcuts=lambda: {"tgcc": "/tmp"},
            create_new_session=lambda _tid, _name, _cwd: ("n", "sid"),
            get_session_cwd=lambda _sid: "/tmp",
        )
        assert "Directory not found" in result.reply_text

    def test_no_args_creates_session(self):
        result = run_new_session_use_case(
            topic_id=0,
            args=[],
            resolve_cwd=lambda _arg: None,
            get_project_shortcuts=lambda: {},
            create_new_session=lambda _tid, _name, cwd=None: ("s1", "sid1"),
            get_session_cwd=lambda _sid: "/tmp",
        )
        assert result.ok
        assert "s1" in result.reply_text


class TestClearSession:
    def test_target_not_found(self):
        result = run_clear_session_use_case(
            topic_id=0,
            args=["ghost"],
            topic_all_sessions={0: {"s1": "sid1"}},
            clear_session=lambda _tid, _name: ("s2", "sid2", None),
            get_session_cwd=lambda _sid: "/tmp",
        )
        assert not result.ok
        assert "not found" in result.reply_text


class TestDeleteSession:
    def test_usage_no_args(self):
        result = run_delete_session_use_case(
            topic_id=0,
            args=[],
            delete_session=lambda _tid, _name: (False, "x", None),
            topic_active_session={},
            topic_all_sessions={},
        )
        assert "Usage: `/delete <session_name>`" in result.reply_text


class TestSessionsList:
    def test_no_sessions(self):
        result = run_sessions_use_case(
            topic_id=0,
            list_sessions=lambda _tid: [],
            session_sdk_ids={},
            get_session_cwd=lambda _sid: "/tmp",
        )
        assert "No sessions yet" in result.reply_text


class TestStatus:
    def test_basic_render(self):
        result = run_status_use_case(
            topic_id=0,
            topic_all_sessions={0: {"main": "sid-main"}},
            topic_active_session={0: "main"},
            session_locks={},
            topic_names={},
            get_session_cwd=lambda _sid: "/tmp/work",
            work_dir="/tmp/work",
            timeout_seconds=0,
            now_str="2026-03-05 17:00:00",
        )
        assert "Bot Status" in result.reply_text
        assert "main" in result.reply_text


class TestTopic:
    def test_no_sessions(self):
        result = run_topic_use_case(
            topic_id=0,
            args=[],
            topic_all_sessions={},
            topic_active_session={},
            topic_names={},
            session_locks={},
            session_sdk_ids={},
            get_session_cwd=lambda _sid: "/tmp",
        )
        assert "No sessions yet" in result.reply_text


class TestCd:
    def test_no_args_global_cwd(self):
        result = run_cd_use_case(
            topic_id=0,
            args=[],
            work_dir="/tmp/work",
            topic_active_session={},
            topic_all_sessions={},
            session_work_dirs={},
            session_cwd_locked={},
            get_session_cwd=lambda _sid: "/tmp/work",
            resolve_cwd=lambda _arg: None,
            get_project_shortcuts=lambda: {},
        )
        assert "Global cwd" in result.reply_text


class TestSessionInfo:
    def test_no_active_session(self):
        result = run_session_info_use_case(
            topic_id=0,
            topic_active_session={},
            topic_all_sessions={},
            session_sdk_ids={},
            session_work_dirs={},
            session_cwd_locked={},
            get_session_cwd=lambda _sid: "/tmp",
        )
        assert "No active session" in result.reply_text


class TestKill:
    def test_no_sessions(self):
        result = run_kill_use_case(
            topic_id=0,
            args=[],
            topic_all_sessions={},
            topic_active_session={},
            session_clients={},
        )
        assert not result.should_interrupt
        assert result.reply_text == "No sessions."


class TestSync:
    def test_init_fallback_to_work_dir(self):
        result = run_sync_init_use_case(
            topic_id=0,
            topic_active_session={},
            topic_all_sessions={},
            get_session_cwd=lambda _sid: "/tmp",
            work_dir="/tmp/work",
        )
        assert result.cwd == "/tmp/work"

    def test_finish_success(self):
        result = run_sync_finish_use_case("synced", "~")
        assert "Memory notebook synced" in result.reply_text


class TestHistoryResolve:
    def test_no_sessions(self):
        result = run_history_resolve_use_case(
            topic_id=0,
            args=[],
            topic_all_sessions={},
            topic_active_session={},
            session_sdk_ids={},
        )
        assert not result.ok
        assert "No sessions." in result.reply_text
