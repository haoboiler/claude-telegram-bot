from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from typing import Any, Optional

from ..memory.session_repository import InMemorySessionRepository


class _AutoPersistDict(dict):
    """Dict that triggers callback on mutating operations."""

    def __init__(self, *args, on_change=None, **kwargs):
        self._on_change = on_change
        super().__init__(*args, **kwargs)

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._changed()

    def clear(self):
        super().clear()
        self._changed()

    _MISSING = object()

    def pop(self, key, default=_MISSING):
        if key in self:
            value = super().pop(key)
            self._changed()
            return value
        if default is self._MISSING:
            raise KeyError(key)
        return default

    def popitem(self):
        item = super().popitem()
        self._changed()
        return item

    def setdefault(self, key, default=None):
        if key in self:
            return self[key]
        value = super().setdefault(key, default)
        self._changed()
        return value

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._changed()


class SqliteSessionRepository:
    """Session repository with SQLite-backed durable state.

    Uses composition over inheritance: wraps an InMemorySessionRepository for
    all business logic, while adding SQLite persistence for durable state.

    Runtime-only state (locks, pending queue, process handles) remains in-memory.
    Durable session mappings are persisted in SQLite and reloaded on startup.
    """

    _DURABLE_KEYS = (
        "topic_active_session",
        "topic_all_sessions",
        "session_sdk_ids",
        "topic_session_counter",
        "topic_names",
        "session_work_dirs",
        "session_cwd_locked",
    )

    def __init__(self, db_path: str) -> None:
        self._memory = InMemorySessionRepository()
        self.db_path = os.path.abspath(db_path)
        self._db_lock = threading.Lock()
        self._persist_enabled = False

        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._ensure_schema()
        self._load_state()
        self._install_auto_persist_wrappers()
        self._persist_enabled = True

    # ── Proxied dict attributes (durable — persisted to SQLite) ──────────

    @property
    def topic_active_session(self) -> dict[int, str]:
        return self._memory.topic_active_session

    @topic_active_session.setter
    def topic_active_session(self, value: dict[int, str]) -> None:
        self._memory.topic_active_session = value

    @property
    def topic_all_sessions(self) -> dict[int, dict[str, str]]:
        return self._memory.topic_all_sessions

    @topic_all_sessions.setter
    def topic_all_sessions(self, value: dict[int, dict[str, str]]) -> None:
        self._memory.topic_all_sessions = value

    @property
    def session_sdk_ids(self) -> dict[str, str]:
        return self._memory.session_sdk_ids

    @session_sdk_ids.setter
    def session_sdk_ids(self, value: dict[str, str]) -> None:
        self._memory.session_sdk_ids = value

    @property
    def topic_session_counter(self) -> dict[int, int]:
        return self._memory.topic_session_counter

    @topic_session_counter.setter
    def topic_session_counter(self, value: dict[int, int]) -> None:
        self._memory.topic_session_counter = value

    @property
    def topic_names(self) -> dict[int, str]:
        return self._memory.topic_names

    @topic_names.setter
    def topic_names(self, value: dict[int, str]) -> None:
        self._memory.topic_names = value

    @property
    def session_work_dirs(self) -> dict[str, str]:
        return self._memory.session_work_dirs

    @session_work_dirs.setter
    def session_work_dirs(self, value: dict[str, str]) -> None:
        self._memory.session_work_dirs = value

    @property
    def session_cwd_locked(self) -> dict[str, bool]:
        return self._memory.session_cwd_locked

    @session_cwd_locked.setter
    def session_cwd_locked(self, value: dict[str, bool]) -> None:
        self._memory.session_cwd_locked = value

    # ── Proxied dict attributes (runtime-only — not persisted) ───────────

    @property
    def session_locks(self) -> dict[str, asyncio.Lock]:
        return self._memory.session_locks

    @session_locks.setter
    def session_locks(self, value: dict[str, asyncio.Lock]) -> None:
        self._memory.session_locks = value

    @property
    def session_pending(self) -> dict[str, int]:
        return self._memory.session_pending

    @session_pending.setter
    def session_pending(self, value: dict[str, int]) -> None:
        self._memory.session_pending = value

    @property
    def session_clients(self) -> dict[str, object]:
        return self._memory.session_clients

    @session_clients.setter
    def session_clients(self, value: dict[str, object]) -> None:
        self._memory.session_clients = value

    # ── Delegated methods (pure logic in InMemorySessionRepository) ──────

    def get_session_lock(self, session_id: str) -> asyncio.Lock:
        return self._memory.get_session_lock(session_id)

    def next_default_name(self, topic_id: int) -> str:
        return self._memory.next_default_name(topic_id)

    def get_session_cwd(self, session_id: str, default_work_dir: str) -> str:
        return self._memory.get_session_cwd(session_id, default_work_dir)

    def get_or_create_session(self, topic_id: int, logger=None) -> str:
        sid = self._memory.get_or_create_session(topic_id, logger=logger)
        self._persist_state()
        return sid

    def create_new_session(
        self,
        topic_id: int,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        logger=None,
    ) -> tuple[str, str]:
        result = self._memory.create_new_session(
            topic_id, name=name, cwd=cwd, logger=logger
        )
        self._persist_state()
        return result

    def switch_session(
        self, topic_id: int, name: str, logger=None
    ) -> Optional[str]:
        result = self._memory.switch_session(topic_id, name, logger=logger)
        if result is not None:
            self._persist_state()
        return result

    def clear_session(
        self,
        topic_id: int,
        target_name: Optional[str] = None,
        logger=None,
    ) -> tuple[str, str, Optional[str]]:
        result = self._memory.clear_session(
            topic_id, target_name=target_name, logger=logger
        )
        self._persist_state()
        return result

    def delete_session(
        self,
        topic_id: int,
        name: str,
        default_work_dir: str,
        logger=None,
    ) -> tuple[bool, str, Optional[str]]:
        result = self._memory.delete_session(
            topic_id, name, default_work_dir=default_work_dir, logger=logger
        )
        if result[0]:
            self._persist_state()
        return result

    def list_sessions(
        self, topic_id: int
    ) -> list[tuple[str, str, bool, bool]]:
        return self._memory.list_sessions(topic_id)

    def resolve_session_target(
        self,
        topic_id: int,
        text: str,
        default_work_dir: str,
        logger=None,
    ) -> tuple[str, str, str]:
        result = self._memory.resolve_session_target(
            topic_id, text, default_work_dir, logger=logger
        )
        self._persist_state()
        return result

    def flush(self) -> None:
        """Explicitly persist current durable state."""
        self._persist_state()

    # ── SQLite persistence internals ─────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _to_int_key_dict(raw: Any) -> dict[int, Any]:
        if not isinstance(raw, dict):
            return {}
        out: dict[int, Any] = {}
        for k, v in raw.items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
        return out

    def _load_state(self) -> None:
        with self._db_lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM session_state"
                ).fetchall()

        if not rows:
            return

        raw: dict[str, Any] = {}
        for key, value in rows:
            try:
                raw[key] = json.loads(value)
            except json.JSONDecodeError:
                continue

        self._memory.topic_active_session = {
            k: str(v)
            for k, v in self._to_int_key_dict(
                raw.get("topic_active_session", {})
            ).items()
        }

        raw_all_sessions = self._to_int_key_dict(
            raw.get("topic_all_sessions", {})
        )
        self._memory.topic_all_sessions = {}
        for topic_id, sessions in raw_all_sessions.items():
            if not isinstance(sessions, dict):
                continue
            self._memory.topic_all_sessions[topic_id] = {
                str(name): str(sid) for name, sid in sessions.items()
            }

        self._memory.session_sdk_ids = {
            str(k): str(v)
            for k, v in (raw.get("session_sdk_ids", {}) or {}).items()
        }

        self._memory.topic_session_counter = {
            k: int(v)
            for k, v in self._to_int_key_dict(
                raw.get("topic_session_counter", {})
            ).items()
        }

        self._memory.topic_names = {
            k: str(v)
            for k, v in self._to_int_key_dict(
                raw.get("topic_names", {})
            ).items()
        }

        self._memory.session_work_dirs = {
            str(k): str(v)
            for k, v in (raw.get("session_work_dirs", {}) or {}).items()
        }

        self._memory.session_cwd_locked = {
            str(k): bool(v)
            for k, v in (raw.get("session_cwd_locked", {}) or {}).items()
        }

    def _snapshot(self) -> dict[str, Any]:
        m = self._memory
        return {
            "topic_active_session": {
                str(k): v for k, v in m.topic_active_session.items()
            },
            "topic_all_sessions": {
                str(k): {name: sid for name, sid in sessions.items()}
                for k, sessions in m.topic_all_sessions.items()
            },
            "session_sdk_ids": dict(m.session_sdk_ids),
            "topic_session_counter": {
                str(k): v for k, v in m.topic_session_counter.items()
            },
            "topic_names": {
                str(k): v for k, v in m.topic_names.items()
            },
            "session_work_dirs": dict(m.session_work_dirs),
            "session_cwd_locked": dict(m.session_cwd_locked),
        }

    def _persist_state(self) -> None:
        if not self._persist_enabled:
            return

        snapshot = self._snapshot()
        encoded = {
            key: json.dumps(snapshot[key], ensure_ascii=False, sort_keys=True)
            for key in self._DURABLE_KEYS
        }

        with self._db_lock:
            with self._connect() as conn:
                for key, value in encoded.items():
                    conn.execute(
                        """
                        INSERT INTO session_state(key, value)
                        VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (key, value),
                    )

                existing_keys = {
                    row[0]
                    for row in conn.execute(
                        "SELECT key FROM session_state"
                    ).fetchall()
                }
                for stale_key in existing_keys.difference(self._DURABLE_KEYS):
                    conn.execute(
                        "DELETE FROM session_state WHERE key = ?",
                        (stale_key,),
                    )
                conn.commit()

    def _install_auto_persist_wrappers(self) -> None:
        """Replace _memory's durable dicts with auto-persisting variants."""
        m = self._memory
        m.topic_active_session = _AutoPersistDict(
            m.topic_active_session, on_change=self._persist_state,
        )
        m.topic_all_sessions = _AutoPersistDict(
            m.topic_all_sessions, on_change=self._persist_state,
        )
        m.session_sdk_ids = _AutoPersistDict(
            m.session_sdk_ids, on_change=self._persist_state,
        )
        m.topic_session_counter = _AutoPersistDict(
            m.topic_session_counter, on_change=self._persist_state,
        )
        m.topic_names = _AutoPersistDict(
            m.topic_names, on_change=self._persist_state,
        )
        m.session_work_dirs = _AutoPersistDict(
            m.session_work_dirs, on_change=self._persist_state,
        )
        m.session_cwd_locked = _AutoPersistDict(
            m.session_cwd_locked, on_change=self._persist_state,
        )
