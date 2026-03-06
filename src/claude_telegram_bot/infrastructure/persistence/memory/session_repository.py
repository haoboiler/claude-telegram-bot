from __future__ import annotations

import asyncio
import uuid
from typing import Optional


class InMemorySessionRepository:
    """In-memory session repository preserving current bot semantics."""

    def __init__(self) -> None:
        self.topic_active_session: dict[int, str] = {}
        self.topic_all_sessions: dict[int, dict[str, str]] = {}
        self.session_sdk_ids: dict[str, str] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.session_pending: dict[str, int] = {}
        self.session_clients: dict[str, object] = {}
        self.topic_session_counter: dict[int, int] = {}
        self.topic_names: dict[int, str] = {}
        self.session_work_dirs: dict[str, str] = {}

    def get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self.session_locks:
            self.session_locks[session_id] = asyncio.Lock()
        return self.session_locks[session_id]

    def next_default_name(self, topic_id: int) -> str:
        cnt = self.topic_session_counter.get(topic_id, 0) + 1
        self.topic_session_counter[topic_id] = cnt
        return f"s{cnt}"

    def get_session_cwd(self, session_id: str, default_work_dir: str) -> str:
        return self.session_work_dirs.get(session_id, default_work_dir)

    def get_or_create_session(self, topic_id: int, logger=None) -> str:
        if topic_id not in self.topic_all_sessions:
            self.topic_all_sessions[topic_id] = {}

        active_name = self.topic_active_session.get(topic_id)
        if active_name and active_name in self.topic_all_sessions[topic_id]:
            return self.topic_all_sessions[topic_id][active_name]

        name = self.next_default_name(topic_id)
        sid = str(uuid.uuid4())
        self.topic_all_sessions[topic_id][name] = sid
        self.topic_active_session[topic_id] = name
        if logger:
            logger.info(f"New session for topic {topic_id}: {name} ({sid})")
        return sid

    def create_new_session(
        self,
        topic_id: int,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        logger=None,
    ) -> tuple[str, str]:
        if topic_id not in self.topic_all_sessions:
            self.topic_all_sessions[topic_id] = {}

        if not name:
            name = self.next_default_name(topic_id)

        if name in self.topic_all_sessions[topic_id]:
            base = name
            i = 2
            while f"{base}{i}" in self.topic_all_sessions[topic_id]:
                i += 1
            name = f"{base}{i}"

        sid = str(uuid.uuid4())
        self.topic_all_sessions[topic_id][name] = sid
        self.topic_active_session[topic_id] = name

        if cwd:
            self.session_work_dirs[sid] = cwd

        if logger:
            logger.info(
                f"Created session for topic {topic_id}: {name} ({sid})"
                f"{f' cwd={cwd}' if cwd else ''}"
            )
        return name, sid

    def switch_session(self, topic_id: int, name: str, logger=None) -> Optional[str]:
        sessions = self.topic_all_sessions.get(topic_id, {})
        if name not in sessions:
            return None
        self.topic_active_session[topic_id] = name
        if logger:
            logger.info(f"Topic {topic_id} switched to session: {name}")
        return sessions[name]

    def clear_session(
        self,
        topic_id: int,
        target_name: Optional[str] = None,
        logger=None,
    ) -> tuple[str, str, Optional[str]]:
        sessions = self.topic_all_sessions.get(topic_id, {})

        if target_name and target_name in sessions:
            old_sid = sessions[target_name]
            old_cwd = self.session_work_dirs.get(old_sid)
            self.session_sdk_ids.pop(old_sid, None)
            self.session_work_dirs.pop(old_sid, None)
            self.session_locks.pop(old_sid, None)
            self.session_pending.pop(old_sid, None)
            self.session_clients.pop(old_sid, None)

            new_sid = str(uuid.uuid4())
            sessions[target_name] = new_sid
            if old_cwd:
                self.session_work_dirs[new_sid] = old_cwd
            if logger:
                logger.info(
                    f"Session '{target_name}' cleared for topic {topic_id}, "
                    f"new sid: {new_sid} (kept name and cwd={old_cwd})"
                )
            return target_name, new_sid, old_cwd

        old_name = self.topic_active_session.get(topic_id)
        old_cwd = None
        if old_name and old_name in sessions:
            old_sid = sessions.pop(old_name)
            old_cwd = self.session_work_dirs.get(old_sid)
            self.session_sdk_ids.pop(old_sid, None)
            self.session_work_dirs.pop(old_sid, None)
            self.session_locks.pop(old_sid, None)
            self.session_pending.pop(old_sid, None)
            self.session_clients.pop(old_sid, None)

        name = self.next_default_name(topic_id)
        sid = str(uuid.uuid4())
        if topic_id not in self.topic_all_sessions:
            self.topic_all_sessions[topic_id] = {}
        self.topic_all_sessions[topic_id][name] = sid
        self.topic_active_session[topic_id] = name
        if logger:
            logger.info(f"Session cleared for topic {topic_id}, new: {name} ({sid})")
        return name, sid, old_cwd

    def delete_session(
        self,
        topic_id: int,
        name: str,
        default_work_dir: str,
        logger=None,
    ) -> tuple[bool, str, Optional[str]]:
        sessions = self.topic_all_sessions.get(topic_id, {})
        if name not in sessions:
            return False, f"Session `{name}` not found.", None

        sid = sessions[name]
        lock = self.session_locks.get(sid)
        if lock and lock.locked():
            return False, f"Session `{name}` is busy. Use /kill first.", None

        deleted_cwd = self.get_session_cwd(sid, default_work_dir)

        sessions.pop(name)
        self.session_sdk_ids.pop(sid, None)
        self.session_work_dirs.pop(sid, None)
        self.session_locks.pop(sid, None)
        self.session_pending.pop(sid, None)
        self.session_clients.pop(sid, None)

        if self.topic_active_session.get(topic_id) == name:
            if sessions:
                new_active = next(iter(sessions))
                self.topic_active_session[topic_id] = new_active
            else:
                new_name = self.next_default_name(topic_id)
                new_sid = str(uuid.uuid4())
                sessions[new_name] = new_sid
                self.topic_active_session[topic_id] = new_name

        if logger:
            logger.info(f"Deleted session '{name}' ({sid[:8]}) for topic {topic_id}")
        return True, f"Session `{name}` deleted.", deleted_cwd

    def list_sessions(self, topic_id: int) -> list[tuple[str, str, bool, bool]]:
        sessions = self.topic_all_sessions.get(topic_id, {})
        active = self.topic_active_session.get(topic_id)
        result = []
        for name, sid in sessions.items():
            lock = self.session_locks.get(sid)
            is_busy = lock.locked() if lock else False
            result.append((name, sid, name == active, is_busy))
        return result

    def resolve_session_target(
        self,
        topic_id: int,
        text: str,
        default_work_dir: str,
        logger=None,
    ) -> tuple[str, str, str]:
        if text.startswith("@") and " " in text:
            target_name, prompt = text.split(" ", 1)
            target_name = target_name[1:]

            sessions = self.topic_all_sessions.get(topic_id, {})
            if target_name in sessions:
                sid = sessions[target_name]
                return target_name, sid, prompt

            prev_active = self.topic_active_session.get(topic_id)
            inherit_cwd = None
            if prev_active and prev_active in sessions:
                active_sid = sessions[prev_active]
                inherit_cwd = self.session_work_dirs.get(active_sid)
            name, sid = self.create_new_session(
                topic_id, target_name, cwd=inherit_cwd, logger=logger
            )
            if prev_active:
                self.topic_active_session[topic_id] = prev_active
            if logger:
                logger.info(
                    f"Auto-created session '{name}' for @mention routing"
                    f"{f' (inherited cwd={inherit_cwd})' if inherit_cwd else ''}"
                )
            return name, sid, prompt

        sid = self.get_or_create_session(topic_id, logger=logger)
        active_name = self.topic_active_session.get(topic_id, "?")
        _ = default_work_dir
        return active_name, sid, text
