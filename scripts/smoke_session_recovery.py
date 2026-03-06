#!/usr/bin/env python3
"""SQLite session recovery smoke checks for Phase 3."""

from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.claude_telegram_bot.infrastructure.persistence.sqlite.session_repository import (
    SqliteSessionRepository,
)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "sessions.sqlite")

        # Boot 1: create baseline sessions
        repo1 = SqliteSessionRepository(db_path)
        _, sid_main = repo1.create_new_session(0, "main", cwd="/tmp/main")
        repo1.session_sdk_ids[sid_main] = "sdk-main"
        repo1.topic_names[0] = "topic-0"
        _, sid_research = repo1.create_new_session(0, "research", cwd="/tmp/research")
        repo1.switch_session(0, "main")
        repo1.flush()

        # Boot 2: verify restore
        repo2 = SqliteSessionRepository(db_path)
        expect(repo2.topic_active_session.get(0) == "main", "restore active session")
        expect(repo2.topic_all_sessions.get(0, {}).get("main") == sid_main, "restore main sid")
        expect(
            repo2.topic_all_sessions.get(0, {}).get("research") == sid_research,
            "restore research sid",
        )
        expect(repo2.session_work_dirs.get(sid_main) == "/tmp/main", "restore main cwd")
        expect(repo2.session_work_dirs.get(sid_research) == "/tmp/research", "restore research cwd")
        expect(repo2.session_sdk_ids.get(sid_main) == "sdk-main", "restore sdk sid")
        expect(repo2.topic_names.get(0) == "topic-0", "restore topic name")

        # Named clear should keep name and cwd but rotate session id
        _, sid_main_new, old_cwd = repo2.clear_session(0, "main")
        expect(old_cwd == "/tmp/main", "clear named old cwd")
        expect(sid_main_new != sid_main, "clear named rotates sid")
        expect(repo2.topic_all_sessions[0]["main"] == sid_main_new, "clear named mapping")
        expect(repo2.session_work_dirs.get(sid_main_new) == "/tmp/main", "clear named keep cwd")
        expect(sid_main not in repo2.session_sdk_ids, "clear named removes old sdk sid")
        repo2.flush()

        # Boot 3: verify post-clear persistence + delete path
        repo3 = SqliteSessionRepository(db_path)
        expect(repo3.topic_all_sessions.get(0, {}).get("main") == sid_main_new, "restore post-clear sid")
        deleted_ok, _msg, deleted_cwd = repo3.delete_session(0, "research", default_work_dir="/tmp/default")
        expect(deleted_ok is True, "delete session success")
        expect(deleted_cwd == "/tmp/research", "delete session cwd")
        repo3.flush()

        # Boot 4: verify post-delete persistence
        repo4 = SqliteSessionRepository(db_path)
        expect("research" not in repo4.topic_all_sessions.get(0, {}), "restore post-delete")

    print("smoke_session_recovery: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
