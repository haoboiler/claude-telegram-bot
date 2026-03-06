"""Tests for SessionRepository Protocol and implementations."""

from __future__ import annotations

import pytest

from src.claude_telegram_bot.domain.repositories.session_repository import (
    SessionRepository,
)
from src.claude_telegram_bot.infrastructure.persistence.memory.session_repository import (
    InMemorySessionRepository,
)


class TestProtocolConformance:
    """Verify implementations satisfy the SessionRepository Protocol."""

    def test_memory_repo_is_runtime_checkable(self):
        repo = InMemorySessionRepository()
        assert isinstance(repo, SessionRepository)


class TestInMemorySessionRepository:
    def test_get_or_create_first_session(self):
        repo = InMemorySessionRepository()
        sid = repo.get_or_create_session(topic_id=0)
        assert sid is not None
        assert 0 in repo.topic_active_session
        assert 0 in repo.topic_all_sessions

    def test_create_new_session(self):
        repo = InMemorySessionRepository()
        name, sid = repo.create_new_session(topic_id=0, name="test")
        assert name == "test"
        assert sid is not None
        assert repo.topic_all_sessions[0]["test"] == sid

    def test_create_duplicate_name_auto_renames(self):
        repo = InMemorySessionRepository()
        name1, _ = repo.create_new_session(topic_id=0, name="x")
        name2, _ = repo.create_new_session(topic_id=0, name="x")
        assert name1 == "x"
        assert name2 == "x2"

    def test_switch_session(self):
        repo = InMemorySessionRepository()
        repo.create_new_session(topic_id=0, name="a")
        repo.create_new_session(topic_id=0, name="b")
        sid = repo.switch_session(topic_id=0, name="a")
        assert sid is not None
        assert repo.topic_active_session[0] == "a"

    def test_switch_nonexistent_returns_none(self):
        repo = InMemorySessionRepository()
        result = repo.switch_session(topic_id=0, name="ghost")
        assert result is None

    def test_clear_session_by_name(self):
        repo = InMemorySessionRepository()
        _, old_sid = repo.create_new_session(topic_id=0, name="main")
        name, new_sid, _ = repo.clear_session(topic_id=0, target_name="main")
        assert name == "main"
        assert new_sid != old_sid

    def test_delete_session(self):
        repo = InMemorySessionRepository()
        repo.create_new_session(topic_id=0, name="del")
        ok, msg, _ = repo.delete_session(
            topic_id=0, name="del", default_work_dir="/tmp"
        )
        assert ok
        assert "del" not in repo.topic_all_sessions.get(0, {})

    def test_delete_nonexistent_fails(self):
        repo = InMemorySessionRepository()
        ok, msg, _ = repo.delete_session(
            topic_id=0, name="ghost", default_work_dir="/tmp"
        )
        assert not ok
        assert "not found" in msg

    def test_list_sessions(self):
        repo = InMemorySessionRepository()
        repo.create_new_session(topic_id=0, name="a")
        repo.create_new_session(topic_id=0, name="b")
        sessions = repo.list_sessions(topic_id=0)
        names = [s[0] for s in sessions]
        assert "a" in names
        assert "b" in names

    def test_resolve_session_target_active(self):
        repo = InMemorySessionRepository()
        repo.create_new_session(topic_id=0, name="main")
        name, sid, prompt = repo.resolve_session_target(
            topic_id=0, text="hello", default_work_dir="/tmp"
        )
        assert name == "main"
        assert prompt == "hello"

    def test_resolve_session_target_at_mention(self):
        repo = InMemorySessionRepository()
        repo.create_new_session(topic_id=0, name="main")
        name, sid, prompt = repo.resolve_session_target(
            topic_id=0, text="@other do stuff", default_work_dir="/tmp"
        )
        assert name == "other"
        assert prompt == "do stuff"

    def test_session_cwd(self):
        repo = InMemorySessionRepository()
        _, sid = repo.create_new_session(topic_id=0, name="x", cwd="/custom")
        assert repo.get_session_cwd(sid, "/default") == "/custom"

    def test_session_cwd_fallback(self):
        repo = InMemorySessionRepository()
        assert repo.get_session_cwd("nonexistent", "/default") == "/default"

    def test_session_lock(self):
        repo = InMemorySessionRepository()
        lock = repo.get_session_lock("sid1")
        assert lock is repo.get_session_lock("sid1")

    def test_next_default_name(self):
        repo = InMemorySessionRepository()
        assert repo.next_default_name(0) == "s1"
        assert repo.next_default_name(0) == "s2"
        assert repo.next_default_name(1) == "s1"
