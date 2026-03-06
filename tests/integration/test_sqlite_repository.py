"""Integration tests for SQLite session repository persistence."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.claude_telegram_bot.domain.repositories.session_repository import (
    SessionRepository,
)
from src.claude_telegram_bot.infrastructure.persistence.sqlite.session_repository import (
    SqliteSessionRepository,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_sessions.sqlite")


class TestSqliteProtocolConformance:
    def test_satisfies_protocol(self, db_path):
        repo = SqliteSessionRepository(db_path)
        assert isinstance(repo, SessionRepository)


class TestSqliteDurability:
    """Verify data survives across repository instances (simulating restart)."""

    def test_session_mapping_persists(self, db_path):
        repo = SqliteSessionRepository(db_path)
        _, sid = repo.create_new_session(0, "main", cwd="/tmp/work")
        repo.session_sdk_ids[sid] = "sdk-main"
        repo.topic_names[0] = "topic-main"
        repo.flush()

        reloaded = SqliteSessionRepository(db_path)
        assert reloaded.topic_active_session.get(0) == "main"
        assert reloaded.topic_all_sessions.get(0, {}).get("main") == sid
        assert reloaded.session_work_dirs.get(sid) == "/tmp/work"
        assert reloaded.session_sdk_ids.get(sid) == "sdk-main"
        assert reloaded.topic_names.get(0) == "topic-main"

    def test_clear_and_reload(self, db_path):
        repo = SqliteSessionRepository(db_path)
        _, sid1 = repo.create_new_session(0, "main")
        _, new_sid, _ = repo.clear_session(0, target_name="main")
        repo.flush()

        reloaded = SqliteSessionRepository(db_path)
        # After clear, the session name should still exist but with a new sid
        assert reloaded.topic_all_sessions[0]["main"] != sid1
        assert reloaded.topic_all_sessions[0]["main"] == new_sid

    def test_delete_and_reload(self, db_path):
        repo = SqliteSessionRepository(db_path)
        repo.create_new_session(0, "a")
        repo.create_new_session(0, "b")
        ok, _, _ = repo.delete_session(0, "a", default_work_dir="/tmp")
        assert ok
        repo.flush()

        reloaded = SqliteSessionRepository(db_path)
        assert "a" not in reloaded.topic_all_sessions.get(0, {})
        assert "b" in reloaded.topic_all_sessions.get(0, {})

    def test_auto_persist_on_dict_mutation(self, db_path):
        """Changes via direct dict mutation should auto-persist."""
        repo = SqliteSessionRepository(db_path)
        repo.create_new_session(0, "main")
        repo.topic_names[0] = "auto-saved"
        # No explicit flush — auto-persist should handle it

        reloaded = SqliteSessionRepository(db_path)
        assert reloaded.topic_names.get(0) == "auto-saved"

    def test_runtime_only_state_not_persisted(self, db_path):
        """Locks and pending counts should not survive restart."""
        repo = SqliteSessionRepository(db_path)
        _, sid = repo.create_new_session(0, "main")
        _ = repo.get_session_lock(sid)
        repo.session_pending[sid] = 3
        repo.flush()

        reloaded = SqliteSessionRepository(db_path)
        assert sid not in reloaded.session_locks
        assert sid not in reloaded.session_pending


class TestSqliteRecoveryChain:
    """Full restart → create → restart → recover → clear → restart → recover → delete → restart chain."""

    def test_full_recovery_chain(self, db_path):
        # Boot 1: create
        repo1 = SqliteSessionRepository(db_path)
        _, sid = repo1.create_new_session(0, "main", cwd="/work")
        repo1.session_sdk_ids[sid] = "sdk1"
        repo1.flush()

        # Boot 2: restore
        repo2 = SqliteSessionRepository(db_path)
        assert repo2.topic_active_session[0] == "main"
        assert repo2.topic_all_sessions[0]["main"] == sid
        assert repo2.session_work_dirs[sid] == "/work"
        assert repo2.session_sdk_ids[sid] == "sdk1"

        # Clear
        name, new_sid, old_cwd = repo2.clear_session(0, target_name="main")
        assert name == "main"
        assert new_sid != sid
        assert old_cwd == "/work"
        repo2.flush()

        # Boot 3: restore after clear
        repo3 = SqliteSessionRepository(db_path)
        assert repo3.topic_all_sessions[0]["main"] == new_sid
        # Old SDK ID should be gone
        assert sid not in repo3.session_sdk_ids

        # Delete
        ok, _, _ = repo3.delete_session(0, "main", default_work_dir="/tmp")
        assert ok
        repo3.flush()

        # Boot 4: restore after delete
        repo4 = SqliteSessionRepository(db_path)
        assert "main" not in repo4.topic_all_sessions.get(0, {})
