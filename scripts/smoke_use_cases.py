#!/usr/bin/env python3
"""Minimal regression smoke checks for extracted use cases.

This script is intentionally dependency-light and does not require Telegram or
Claude runtime. It validates core branching behavior for migrated command
use-cases so refactors can run a quick compatibility guard.
"""

from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
from src.claude_telegram_bot.application.use_cases.ask_user import (
    parse_ask_callback_data,
    resolve_ask_selection,
)
from src.claude_telegram_bot.application.use_cases.file_flow import (
    run_prepare_file_caption_use_case,
)
from src.claude_telegram_bot.application.use_cases.message_flow import (
    render_busy_reply,
    render_final_activity_reply,
    render_progress_reply,
    run_label_response_parts_use_case,
    run_prepare_message_use_case,
)
from src.claude_telegram_bot.bootstrap.auth_state import (
    parse_allowed_user_ids,
    resolve_auth_bootstrap_state,
)
from src.claude_telegram_bot.domain.policies.group_auth import (
    is_group_chat,
    should_respond_in_group,
)
from src.claude_telegram_bot.infrastructure.telegram.uploads import (
    resolve_upload_path,
    select_upload_file,
)
from src.claude_telegram_bot.infrastructure.persistence.sqlite.session_repository import (
    SqliteSessionRepository,
)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class Obj:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def main() -> int:
    # /switch: empty sessions
    switch_empty = run_switch_session_use_case(
        topic_id=0,
        args=[],
        list_sessions=lambda _tid: [],
        switch_session=lambda _tid, _name: None,
        topic_all_sessions={},
    )
    expect(switch_empty.reply_text == "No sessions. Send a message to create one.", "switch empty")

    # /new: invalid cwd
    new_invalid = run_new_session_use_case(
        topic_id=0,
        args=["build", "missing_dir"],
        resolve_cwd=lambda _arg: None,
        get_project_shortcuts=lambda: {"tgcc": "/tmp"},
        create_new_session=lambda _tid, _name, _cwd: ("n", "sid"),
        get_session_cwd=lambda _sid: "/tmp",
    )
    expect("Directory not found" in new_invalid.reply_text, "new invalid cwd")

    # /clear: target not found
    clear_missing = run_clear_session_use_case(
        topic_id=0,
        args=["ghost"],
        topic_all_sessions={0: {"s1": "sid1"}},
        clear_session=lambda _tid, _name: ("s2", "sid2", None),
        get_session_cwd=lambda _sid: "/tmp",
    )
    expect(not clear_missing.ok and "not found" in clear_missing.reply_text, "clear missing")

    # /sessions: no session
    sessions_none = run_sessions_use_case(
        topic_id=0,
        list_sessions=lambda _tid: [],
        session_sdk_ids={},
        get_session_cwd=lambda _sid: "/tmp",
    )
    expect("No sessions yet" in sessions_none.reply_text, "sessions empty")

    # /status: basic render
    status = run_status_use_case(
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
    expect("Bot Status" in status.reply_text and "main" in status.reply_text, "status render")

    # /topic: no sessions
    topic_empty = run_topic_use_case(
        topic_id=0,
        args=[],
        topic_all_sessions={},
        topic_active_session={},
        topic_names={},
        session_locks={},
        session_sdk_ids={},
        get_session_cwd=lambda _sid: "/tmp",
    )
    expect("No sessions yet" in topic_empty.reply_text, "topic empty")

    # /cd: no args, no active session -> global cwd
    cd_global_view = run_cd_use_case(
        topic_id=0,
        args=[],
        work_dir="/tmp/work",
        topic_active_session={},
        topic_all_sessions={},
        session_work_dirs={},
        get_session_cwd=lambda _sid: "/tmp/work",
        resolve_cwd=lambda _arg: None,
        get_project_shortcuts=lambda: {},
    )
    expect("Global cwd" in cd_global_view.reply_text, "cd global view")

    # /session: no active
    session_none = run_session_info_use_case(
        topic_id=0,
        topic_active_session={},
        topic_all_sessions={},
        session_sdk_ids={},
        session_work_dirs={},
        get_session_cwd=lambda _sid: "/tmp",
    )
    expect("No active session" in session_none.reply_text, "session empty")

    # /kill: no sessions
    kill_none = run_kill_use_case(
        topic_id=0,
        args=[],
        topic_all_sessions={},
        topic_active_session={},
        session_clients={},
    )
    expect(not kill_none.should_interrupt and kill_none.reply_text == "No sessions.", "kill empty")

    # /delete: usage
    delete_usage = run_delete_session_use_case(
        topic_id=0,
        args=[],
        delete_session=lambda _tid, _name: (False, "x", None),
        topic_active_session={},
        topic_all_sessions={},
    )
    expect("Usage: `/delete <session_name>`" in delete_usage.reply_text, "delete usage")

    # /sync
    sync_init = run_sync_init_use_case(
        topic_id=0,
        topic_active_session={},
        topic_all_sessions={},
        get_session_cwd=lambda _sid: "/tmp",
        work_dir="/tmp/work",
    )
    expect(sync_init.cwd == "/tmp/work", "sync init")
    sync_finish = run_sync_finish_use_case("synced", "~")
    expect("Memory notebook synced" in sync_finish.reply_text, "sync finish")

    # /history: no sessions
    history_none = run_history_resolve_use_case(
        topic_id=0,
        args=[],
        topic_all_sessions={},
        topic_active_session={},
        session_sdk_ids={},
    )
    expect(not history_none.ok and "No sessions." in history_none.reply_text, "history empty")

    # AskUser callback parse + selection
    parsed = parse_ask_callback_data("ask:q1:0")
    expect(parsed == ("q1", "0"), "ask parse ok")
    expect(parse_ask_callback_data("bad:data") is None, "ask parse invalid")
    selected = resolve_ask_selection("1", [{"label": "A"}, {"label": "B"}])
    expect(selected == "B", "ask selection index")

    # Upload helpers
    msg = Obj(
        document=Obj(file_name="a.txt"),
        photo=[Obj(id="small"), Obj(id="big")],
        video=None,
        audio=None,
        voice=None,
        video_note=None,
    )
    file_obj, original_name = select_upload_file(msg, now_ts=123)
    expect(original_name == "a.txt", "upload priority document")
    expect(file_obj.file_name == "a.txt", "upload object selection")
    path_no_conflict = resolve_upload_path("/tmp", "a.txt", exists_fn=lambda _p: False, now_ts=999)
    expect(path_no_conflict == "/tmp/a.txt", "upload path no conflict")
    path_conflict = resolve_upload_path("/tmp", "a.txt", exists_fn=lambda _p: True, now_ts=999)
    expect(path_conflict == "/tmp/a_999.txt", "upload path conflict")

    # Group auth policy
    update_private = Obj(effective_chat=Obj(type="private"), effective_user=Obj(id=1))
    expect(is_group_chat(update_private) is False, "private is not group")
    update_group = Obj(
        effective_chat=Obj(type="supergroup"),
        effective_user=Obj(id=100),
        message=Obj(
            text="/start",
            entities=[],
            reply_to_message=None,
            is_topic_message=False,
            message_thread_id=None,
        ),
        effective_message=None,
    )
    expect(
        should_respond_in_group(
            update_group,
            is_command=True,
            owner_user_id=100,
            allowed_user_ids=set(),
            bot_username="bot",
            logger=None,
        )
        is True,
        "group command owner",
    )

    # Message/file flow use-cases
    prepared_message = run_prepare_message_use_case(
        topic_id=1,
        text="hello",
        reply_context="ctx",
        resolve_session_target=lambda _tid, raw: ("main", "sid-main", f"prompt:{raw}"),
    )
    expect(prepared_message.session_name == "main", "message flow session name")
    expect(prepared_message.session_id == "sid-main", "message flow session id")
    expect(
        prepared_message.prompt == "ctx\n\nUser's message: prompt:hello",
        "message flow prompt",
    )
    expect(
        render_busy_reply("main")
        == "[main] Busy processing previous request. This message was canceled.",
        "message flow busy text",
    )
    expect(
        render_busy_reply("main", is_file_caption=True)
        == "[main] Busy processing previous request. This file+caption message was canceled.",
        "file flow busy text",
    )
    expect(render_progress_reply("main") == "[main] Thinking...", "message flow progress")
    expect(
        render_progress_reply("main", is_file_caption=True)
        == "[main] Processing file + message...",
        "file flow progress",
    )
    expect(
        render_final_activity_reply("main", ["read", "write"])
        == "[main] Done (2 steps)\n  ▸ read\n  ▸ write",
        "message flow final log",
    )
    expect(
        run_label_response_parts_use_case("main", ["first", "second"])
        == ["[main] first", "second"],
        "message flow label parts",
    )

    no_caption_plan = run_prepare_file_caption_use_case(
        topic_id=1,
        caption="",
        save_path="/tmp/a.txt",
        resolve_session_target=lambda _tid, _txt: ("main", "sid-main", "prompt"),
    )
    expect(no_caption_plan.forward_to_claude is False, "file flow no caption forward")
    expect("File saved to" in (no_caption_plan.reply_text or ""), "file flow no caption reply")
    expect(no_caption_plan.parse_mode == "Markdown", "file flow no caption parse mode")

    caption_plan = run_prepare_file_caption_use_case(
        topic_id=1,
        caption="analyze this file",
        save_path="/tmp/a.txt",
        resolve_session_target=lambda _tid, txt: ("main", "sid-main", f"resolved:{txt}"),
    )
    expect(caption_plan.forward_to_claude is True, "file flow caption forward")
    expect(caption_plan.session_name == "main", "file flow caption session name")
    expect(caption_plan.session_id == "sid-main", "file flow caption session id")
    expect(
        caption_plan.prompt_text == "File saved to: /tmp/a.txt\n\nresolved:analyze this file",
        "file flow caption prompt",
    )

    # SQLite session repository durability smoke
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "sessions.sqlite")
        repo = SqliteSessionRepository(db_path)
        _, sid = repo.create_new_session(0, "main", cwd="/tmp/work")
        repo.session_sdk_ids[sid] = "sdk-main"
        repo.topic_names[0] = "topic-main"
        repo.flush()

        reloaded = SqliteSessionRepository(db_path)
        expect(reloaded.topic_active_session.get(0) == "main", "sqlite active session")
        expect(
            reloaded.topic_all_sessions.get(0, {}).get("main") == sid,
            "sqlite session mapping",
        )
        expect(
            reloaded.session_work_dirs.get(sid) == "/tmp/work",
            "sqlite session cwd",
        )
        expect(
            reloaded.session_sdk_ids.get(sid) == "sdk-main",
            "sqlite sdk sid",
        )
        expect(reloaded.topic_names.get(0) == "topic-main", "sqlite topic name")

    # Auth bootstrap precedence
    expect(parse_allowed_user_ids("1,2,3") == {1, 2, 3}, "parse allowed users")
    auth_env = resolve_auth_bootstrap_state(
        owner_env_value="9",
        allowed_env_value="1,2",
        persisted_state={"owner_user_id": 7, "allowed_user_ids": [7]},
    )
    expect(auth_env.owner_user_id == 9, "auth owner env priority")
    expect(auth_env.allowed_user_ids == {1, 2}, "auth allowed env parse")
    expect(auth_env.loaded_owner_from_state is False, "auth no state owner when env")
    expect(auth_env.loaded_allowed_from_state is False, "auth no state allowed when env")

    auth_allowed_derived = resolve_auth_bootstrap_state(
        owner_env_value="",
        allowed_env_value="11,12",
        persisted_state={},
    )
    expect(auth_allowed_derived.owner_user_id in {11, 12}, "auth owner derived from allowed")
    expect(auth_allowed_derived.allowed_user_ids == {11, 12}, "auth allowed from env")

    auth_state = resolve_auth_bootstrap_state(
        owner_env_value="",
        allowed_env_value="",
        persisted_state={"owner_user_id": 5, "allowed_user_ids": [5, 6]},
    )
    expect(auth_state.owner_user_id == 5, "auth owner from state")
    expect(auth_state.allowed_user_ids == {5, 6}, "auth allowed from state")
    expect(auth_state.loaded_owner_from_state is True, "auth owner state flag")
    expect(auth_state.loaded_allowed_from_state is True, "auth allowed state flag")

    print("smoke_use_cases: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
