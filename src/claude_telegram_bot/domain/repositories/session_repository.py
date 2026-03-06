from __future__ import annotations

import asyncio
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class SessionRepository(Protocol):
    """Session state access and mutation boundary."""

    topic_active_session: dict[int, str]
    topic_all_sessions: dict[int, dict[str, str]]
    session_sdk_ids: dict[str, str]
    session_locks: dict[str, asyncio.Lock]
    session_pending: dict[str, int]
    session_clients: dict[str, object]
    topic_session_counter: dict[int, int]
    topic_names: dict[int, str]
    session_work_dirs: dict[str, str]

    def get_session_lock(self, session_id: str) -> asyncio.Lock: ...
    def next_default_name(self, topic_id: int) -> str: ...
    def get_session_cwd(self, session_id: str, default_work_dir: str) -> str: ...
    def get_or_create_session(self, topic_id: int, logger=None) -> str: ...
    def create_new_session(
        self,
        topic_id: int,
        name: Optional[str] = None,
        cwd: Optional[str] = None,
        logger=None,
    ) -> tuple[str, str]: ...
    def switch_session(
        self, topic_id: int, name: str, logger=None
    ) -> Optional[str]: ...
    def clear_session(
        self,
        topic_id: int,
        target_name: Optional[str] = None,
        logger=None,
    ) -> tuple[str, str, Optional[str]]: ...
    def delete_session(
        self,
        topic_id: int,
        name: str,
        default_work_dir: str,
        logger=None,
    ) -> tuple[bool, str, Optional[str]]: ...
    def list_sessions(
        self, topic_id: int
    ) -> list[tuple[str, str, bool, bool]]: ...
    def resolve_session_target(
        self,
        topic_id: int,
        text: str,
        default_work_dir: str,
        logger=None,
    ) -> tuple[str, str, str]: ...
