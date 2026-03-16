from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional


@dataclass
class ClearSessionResult:
    ok: bool
    reply_text: str
    parse_mode: Optional[str]
    old_cwd: Optional[str]


@dataclass
class NewSessionResult:
    ok: bool
    reply_text: str
    parse_mode: Optional[str]


@dataclass
class SwitchSessionResult:
    ok: bool
    reply_text: str
    parse_mode: Optional[str]


@dataclass
class TextResult:
    reply_text: str
    parse_mode: Optional[str]


@dataclass
class CdResult:
    reply_text: str
    parse_mode: Optional[str]
    new_global_work_dir: Optional[str] = None
    session_cwd_update: Optional[tuple[str, str]] = None


@dataclass
class KillDecision:
    should_interrupt: bool
    reply_text: Optional[str]
    parse_mode: Optional[str]
    target_name: Optional[str] = None
    target_sid: Optional[str] = None
    client: Optional[object] = None


@dataclass
class DeleteSessionResult:
    ok: bool
    reply_text: str
    parse_mode: Optional[str]
    deleted_cwd: Optional[str]


@dataclass
class SyncInitResult:
    cwd: str
    display_cwd: str
    initial_reply_text: str
    parse_mode: Optional[str]


@dataclass
class AttachSessionResult:
    ok: bool
    reply_text: str
    parse_mode: Optional[str]
    session_name: Optional[str] = None
    session_id: Optional[str] = None
    sdk_session_id: Optional[str] = None
    cwd: Optional[str] = None


@dataclass
class HistoryResolveResult:
    ok: bool
    reply_text: Optional[str]
    parse_mode: Optional[str]
    n: int = 5
    session_name: Optional[str] = None
    session_id: Optional[str] = None
    sdk_sid: Optional[str] = None


def run_clear_session_use_case(
    *,
    topic_id: int,
    args: list[str],
    topic_all_sessions: dict[int, dict[str, str]],
    clear_session: Callable[[int, str | None], tuple[str, str, str | None]],
    get_session_cwd: Callable[[str], str],
) -> ClearSessionResult:
    target_name = args[0] if args else None

    if target_name:
        sessions = topic_all_sessions.get(topic_id, {})
        if target_name not in sessions:
            available = ", ".join(f"`{n}`" for n in sorted(sessions.keys()))
            return ClearSessionResult(
                ok=False,
                reply_text=(
                    f"Session `{target_name}` not found.\n"
                    f"Available: {available or 'none'}"
                ),
                parse_mode="Markdown",
                old_cwd=None,
            )

    cleared_name, new_sid, old_cwd = clear_session(topic_id, target_name)

    if target_name:
        home = os.path.expanduser("~")
        cwd_display = get_session_cwd(new_sid).replace(home, "~")
        reply = (
            f"Session `{cleared_name}` cleared (context reset).\n"
            f"📁 cwd: `{cwd_display}` (preserved)"
        )
    else:
        reply = (
            f"Conversation cleared.\n"
            f"New session: `{cleared_name}` (`{new_sid[:8]}...`)"
        )

    return ClearSessionResult(
        ok=True,
        reply_text=reply,
        parse_mode="Markdown",
        old_cwd=old_cwd,
    )


def run_new_session_use_case(
    *,
    topic_id: int,
    args: list[str],
    resolve_cwd: Callable[[Optional[str]], Optional[str]],
    get_project_shortcuts: Callable[[], dict[str, str]],
    create_new_session: Callable[[int, Optional[str], Optional[str]], tuple[str, str]],
    get_session_cwd: Callable[[str], str],
) -> NewSessionResult:
    name = None
    cwd = None
    cwd_arg = None

    if args:
        name = args[0]
        if len(args) >= 2:
            cwd_arg = " ".join(args[1:])
            resolved = resolve_cwd(cwd_arg)
            if resolved:
                cwd = resolved
            else:
                available = ", ".join(f"`{k}`" for k in sorted(get_project_shortcuts().keys()))
                return NewSessionResult(
                    ok=False,
                    reply_text=(
                        f"⚠️ Directory not found: `{cwd_arg}`\n\n"
                        f"Available shortcuts: {available}\n"
                        f"Or use an absolute/relative path."
                    ),
                    parse_mode="Markdown",
                )

    new_name, new_sid = create_new_session(topic_id, name, cwd=cwd)
    effective = get_session_cwd(new_sid)
    display_cwd = effective.replace(os.path.expanduser("~"), "~")
    cwd_note = ""
    if cwd and cwd_arg:
        cwd_note = " (shortcut)" if cwd_arg in get_project_shortcuts() else ""

    return NewSessionResult(
        ok=True,
        reply_text=(
            f"✅ New session: `{new_name}` (`{new_sid[:8]}...`)\n"
            f"📁 cwd: `{display_cwd}`{cwd_note}\n\n"
            f"Use /sessions to see all sessions.\n"
            f"Use /switch <name> to switch back."
        ),
        parse_mode="Markdown",
    )


def run_switch_session_use_case(
    *,
    topic_id: int,
    args: list[str],
    list_sessions: Callable[[int], list[tuple[str, str, bool, bool]]],
    switch_session: Callable[[int, str], Optional[str]],
    topic_all_sessions: dict[int, dict[str, str]],
) -> SwitchSessionResult:
    if not args:
        sessions = list_sessions(topic_id)
        if not sessions:
            return SwitchSessionResult(
                ok=False,
                reply_text="No sessions. Send a message to create one.",
                parse_mode=None,
            )
        lines = ["Sessions:"]
        for name, sid, is_active, is_busy in sessions:
            marker = " (active)" if is_active else ""
            busy = " [BUSY]" if is_busy else ""
            lines.append(f"  `{name}` - `{sid[:8]}...`{marker}{busy}")
        lines.append("\nUsage: /switch <name>")
        return SwitchSessionResult(
            ok=True,
            reply_text="\n".join(lines),
            parse_mode="Markdown",
        )

    target = args[0]
    sid = switch_session(topic_id, target)
    if sid:
        return SwitchSessionResult(
            ok=True,
            reply_text=f"Switched to session: `{target}` (`{sid[:8]}...`)",
            parse_mode="Markdown",
        )

    sessions = topic_all_sessions.get(topic_id, {})
    matches = [n for n in sessions if target.lower() in n.lower()]
    if matches:
        hint = ", ".join(f"`{m}`" for m in matches)
        return SwitchSessionResult(
            ok=False,
            reply_text=f"Session `{target}` not found. Did you mean: {hint}?",
            parse_mode="Markdown",
        )
    return SwitchSessionResult(
        ok=False,
        reply_text=f"Session `{target}` not found. Use /sessions to see all.",
        parse_mode="Markdown",
    )


def run_sessions_use_case(
    *,
    topic_id: int,
    list_sessions: Callable[[int], list[tuple[str, str, bool, bool]]],
    session_sdk_ids: dict[str, str],
    get_session_cwd: Callable[[str], str],
) -> TextResult:
    sessions = list_sessions(topic_id)
    if not sessions:
        return TextResult(
            reply_text="No sessions yet. Send a message to start one.",
            parse_mode=None,
        )

    home = os.path.expanduser("~")
    lines = ["All sessions:"]
    for name, sid, is_active, is_busy in sessions:
        marker = " <- active" if is_active else ""
        has_context = sid in session_sdk_ids
        status = "BUSY" if is_busy else ("has context" if has_context else "empty")
        sdk_sid = session_sdk_ids.get(sid)
        sdk_label = f"  sdk:`{sdk_sid[:8]}`" if sdk_sid else ""
        s_cwd = get_session_cwd(sid).replace(home, "~")
        lines.append(f"  `{name}` ({status}){marker}{sdk_label}\n    📁 `{s_cwd}`")
    lines.append(f"\nTotal: {len(sessions)}")
    lines.append(
        "\nQuick reference:\n"
        "  /switch <name> - Switch to session\n"
        "  /new [name] [cwd] - Create new session\n"
        "  /attach <sdk_id> <name> - Attach external session\n"
        "  /delete <name> - Delete a session\n"
        "  /clear [name] - Reset session context\n"
        "  /cd [path] - Change session cwd\n"
        "  /kill [name] - Kill busy session\n"
        "  /sync - Sync memory notebook"
    )
    return TextResult(reply_text="\n".join(lines), parse_mode="Markdown")


def run_status_use_case(
    *,
    topic_id: int,
    topic_all_sessions: dict[int, dict[str, str]],
    topic_active_session: dict[int, str],
    session_locks: dict[str, object],
    topic_names: dict[int, str],
    get_session_cwd: Callable[[str], str],
    work_dir: str,
    timeout_seconds: int,
    now_str: str,
) -> TextResult:
    home = os.path.expanduser("~")

    total_sessions = 0
    busy_list = []
    for tid, sessions in topic_all_sessions.items():
        for name, sid in sessions.items():
            total_sessions += 1
            lock = session_locks.get(sid)
            if lock and lock.locked():
                topic_label = f"[T:{tid}]" if tid else "[Private]"
                busy_list.append(f"{topic_label} {name}")

    current_topic_sessions = topic_all_sessions.get(topic_id, {})
    active_name = topic_active_session.get(topic_id, "none")
    active_sid = current_topic_sessions.get(active_name)
    active_cwd = get_session_cwd(active_sid).replace(home, "~") if active_sid else "N/A"
    global_cwd = work_dir.replace(home, "~")
    topic_label = topic_names.get(topic_id, f"Topic #{topic_id}") if topic_id else "Private chat"

    return TextResult(
        reply_text=(
            f"Bot Status:\n"
            f"- Running: yes\n"
            f"- Context: {topic_label}\n"
            f"- Active session: `{active_name}`\n"
            f"- Session cwd: `{active_cwd}`\n"
            f"- Global default cwd: `{global_cwd}`\n"
            f"- Sessions (this topic): {len(current_topic_sessions)}\n"
            f"- Sessions (all topics): {total_sessions}\n"
            f"- Busy: {', '.join(f'`{n}`' for n in busy_list) if busy_list else 'none'}\n"
            f"- Timeout: {timeout_seconds}s\n"
            f"- Time: {now_str}"
        ),
        parse_mode="Markdown",
    )


def run_topic_use_case(
    *,
    topic_id: int,
    args: list[str],
    topic_all_sessions: dict[int, dict[str, str]],
    topic_active_session: dict[int, str],
    topic_names: dict[int, str],
    session_locks: dict[str, object],
    session_sdk_ids: dict[str, str],
    get_session_cwd: Callable[[str], str],
) -> TextResult:
    subcommand = args[0].lower() if args else "info"

    if subcommand == "list":
        if not topic_all_sessions:
            return TextResult(
                reply_text="No topics with sessions yet.",
                parse_mode=None,
            )
        lines = ["📋 All topics with sessions:"]
        for tid, sessions in sorted(topic_all_sessions.items()):
            if not sessions:
                continue
            active = topic_active_session.get(tid, "?")
            label = topic_names.get(tid, f"Topic #{tid}") if tid else "Private chat"
            busy_count = sum(
                1
                for sid in sessions.values()
                if session_locks.get(sid) and session_locks[sid].locked()
            )
            marker = " <- you are here" if tid == topic_id else ""
            lines.append(
                f"\n  {label}{marker}\n"
                f"    Sessions: {len(sessions)} | Active: `{active}` | Busy: {busy_count}"
            )
            for name, sid in sessions.items():
                is_active = name == active
                prefix = "-> " if is_active else "  "
                lines.append(f"    {prefix}`{name}`")
        return TextResult(reply_text="\n".join(lines), parse_mode="Markdown")

    sessions = topic_all_sessions.get(topic_id, {})
    active_name = topic_active_session.get(topic_id, "none")
    label = topic_names.get(topic_id, f"Topic #{topic_id}") if topic_id else "Private chat"

    if not sessions:
        return TextResult(
            reply_text=f"{label}\nNo sessions yet. Send a message to create one.",
            parse_mode=None,
        )

    home = os.path.expanduser("~")
    lines = [f"📌 {label}\n"]
    for name, sid in sessions.items():
        is_active = name == active_name
        has_context = sid in session_sdk_ids
        lock = session_locks.get(sid)
        is_busy = lock.locked() if lock else False
        status = "BUSY" if is_busy else ("has context" if has_context else "empty")
        prefix = "-> " if is_active else "  "
        s_cwd = get_session_cwd(sid).replace(home, "~")
        lines.append(f"{prefix}`{name}` ({status})\n    📁 `{s_cwd}`")
    lines.append(f"\nTotal: {len(sessions)}")
    return TextResult(reply_text="\n".join(lines), parse_mode="Markdown")


def run_cd_use_case(
    *,
    topic_id: int,
    args: list[str],
    work_dir: str,
    topic_active_session: dict[int, str],
    topic_all_sessions: dict[int, dict[str, str]],
    session_work_dirs: dict[str, str],
    session_cwd_locked: dict[str, bool],
    get_session_cwd: Callable[[str], str],
    resolve_cwd: Callable[[Optional[str]], Optional[str]],
    get_project_shortcuts: Callable[[], dict[str, str]],
) -> CdResult:
    active_name = topic_active_session.get(topic_id)
    sessions = topic_all_sessions.get(topic_id, {})
    active_sid = sessions.get(active_name) if active_name else None
    home = os.path.expanduser("~")

    if not args:
        if active_sid:
            s_cwd = get_session_cwd(active_sid).replace(home, "~")
            is_custom = active_sid in session_work_dirs
            global_cwd = work_dir.replace(home, "~")
            msg = f"Session `{active_name}` cwd: `{s_cwd}`"
            if is_custom:
                msg += f"\nGlobal default: `{global_cwd}`"
        else:
            msg = f"Global cwd: `{work_dir.replace(home, '~')}`"
        return CdResult(reply_text=msg, parse_mode="Markdown")

    parsed_args = list(args)
    is_global = False
    if parsed_args[0] == "--global":
        is_global = True
        parsed_args = parsed_args[1:]
        if not parsed_args:
            return CdResult(
                reply_text=(
                    f"Global default: `{work_dir.replace(home, '~')}`\n"
                    f"Usage: /cd --global <path|shortname>"
                ),
                parse_mode="Markdown",
            )

    cwd_arg = " ".join(parsed_args)
    resolved = resolve_cwd(cwd_arg)

    if not resolved:
        available = ", ".join(f"`{k}`" for k in sorted(get_project_shortcuts().keys()))
        return CdResult(
            reply_text=(
                f"Directory not found: `{cwd_arg}`\n\n"
                f"Available shortcuts: {available}"
            ),
            parse_mode="Markdown",
        )

    display = resolved.replace(home, "~")
    shortcut_note = " (shortcut)" if cwd_arg in get_project_shortcuts() else ""

    if is_global:
        return CdResult(
            reply_text=(
                f"Global default changed to:\n`{display}`{shortcut_note}\n"
                f"(Affects new sessions without custom cwd)"
            ),
            parse_mode="Markdown",
            new_global_work_dir=resolved,
        )

    if not active_sid:
        return CdResult(
            reply_text="No active session. Send a message to create one first.",
            parse_mode=None,
        )

    if active_sid in session_cwd_locked:
        return CdResult(
            reply_text=(
                f"Session `{active_name}` cwd is locked (attached session).\n"
                f"Use /clear to reset the session first."
            ),
            parse_mode="Markdown",
        )

    return CdResult(
        reply_text=f"Session `{active_name}` cwd changed to:\n`{display}`{shortcut_note}",
        parse_mode="Markdown",
        session_cwd_update=(active_sid, resolved),
    )


def run_session_info_use_case(
    *,
    topic_id: int,
    topic_active_session: dict[int, str],
    topic_all_sessions: dict[int, dict[str, str]],
    session_sdk_ids: dict[str, str],
    session_work_dirs: dict[str, str],
    session_cwd_locked: dict[str, bool],
    get_session_cwd: Callable[[str], str],
) -> TextResult:
    active_name = topic_active_session.get(topic_id)
    sessions = topic_all_sessions.get(topic_id, {})
    if active_name and active_name in sessions:
        sid = sessions[active_name]
        has_context = sid in session_sdk_ids
        status = "has context" if has_context else "empty"
        home = os.path.expanduser("~")
        s_cwd = get_session_cwd(sid).replace(home, "~")
        is_custom = sid in session_work_dirs
        is_locked = sid in session_cwd_locked
        cwd_suffix = " (locked)" if is_locked else (" (custom)" if is_custom else " (global)")
        cwd_label = f"`{s_cwd}`{cwd_suffix}"

        lines = [
            f"Current session: `{active_name}` ({status})",
            f"Session ID: `{sid}`",
        ]

        sdk_sid = session_sdk_ids.get(sid)
        if sdk_sid:
            lines.append(f"SDK Session: `{sdk_sid}`")

        lines.append(f"Work dir: {cwd_label}")
        lines.append(f"Total sessions: {len(sessions)}")

        if sdk_sid:
            lines.append(
                f"\nTerminal resume:\n"
                f"`cd {s_cwd} && claude --resume {sdk_sid}`"
            )

        return TextResult(
            reply_text="\n".join(lines),
            parse_mode="Markdown",
        )
    return TextResult(
        reply_text="No active session. Send a message to start one.",
        parse_mode=None,
    )


def run_kill_use_case(
    *,
    topic_id: int,
    args: list[str],
    topic_all_sessions: dict[int, dict[str, str]],
    topic_active_session: dict[int, str],
    session_clients: dict[str, object],
) -> KillDecision:
    sessions = topic_all_sessions.get(topic_id, {})
    if not sessions:
        return KillDecision(
            should_interrupt=False,
            reply_text="No sessions.",
            parse_mode=None,
        )

    if args:
        target_name: Optional[str] = args[0]
    else:
        target_name = topic_active_session.get(topic_id)

    if not target_name or target_name not in sessions:
        busy = []
        for name, sid in sessions.items():
            if sid in session_clients:
                busy.append(name)
        if busy:
            return KillDecision(
                should_interrupt=False,
                reply_text=(
                    f"Session `{target_name}` not found.\n"
                    f"Busy sessions: {', '.join(f'`{n}`' for n in busy)}\n"
                    f"Usage: /kill <session_name>"
                ),
                parse_mode="Markdown",
            )
        return KillDecision(
            should_interrupt=False,
            reply_text="No busy sessions to kill.",
            parse_mode=None,
        )

    sid = sessions[target_name]
    client = session_clients.get(sid)
    if not client:
        return KillDecision(
            should_interrupt=False,
            reply_text=f"Session `{target_name}` is not running.",
            parse_mode="Markdown",
            target_name=target_name,
            target_sid=sid,
        )

    return KillDecision(
        should_interrupt=True,
        reply_text=None,
        parse_mode=None,
        target_name=target_name,
        target_sid=sid,
        client=client,
    )


def run_delete_session_use_case(
    *,
    topic_id: int,
    args: list[str],
    delete_session: Callable[[int, str], tuple[bool, str, Optional[str]]],
    topic_active_session: dict[int, str],
    topic_all_sessions: dict[int, dict[str, str]],
) -> DeleteSessionResult:
    if not args:
        return DeleteSessionResult(
            ok=False,
            reply_text="Usage: `/delete <session_name>`\nUse /sessions to see all sessions.",
            parse_mode="Markdown",
            deleted_cwd=None,
        )

    target_name = args[0]
    success, message, deleted_cwd = delete_session(topic_id, target_name)
    if not success:
        return DeleteSessionResult(
            ok=False,
            reply_text=message,
            parse_mode="Markdown",
            deleted_cwd=None,
        )

    active_name = topic_active_session.get(topic_id, "?")
    remaining = len(topic_all_sessions.get(topic_id, {}))
    reply = f"{message}\nActive: `{active_name}` | Total: {remaining}"
    return DeleteSessionResult(
        ok=True,
        reply_text=reply,
        parse_mode="Markdown",
        deleted_cwd=deleted_cwd,
    )


def run_sync_init_use_case(
    *,
    topic_id: int,
    topic_active_session: dict[int, str],
    topic_all_sessions: dict[int, dict[str, str]],
    get_session_cwd: Callable[[str], str],
    work_dir: str,
) -> SyncInitResult:
    active_name = topic_active_session.get(topic_id)
    sessions = topic_all_sessions.get(topic_id, {})
    if active_name and active_name in sessions:
        sid = sessions[active_name]
        cwd = get_session_cwd(sid)
    else:
        cwd = work_dir

    home = os.path.expanduser("~")
    display_cwd = cwd.replace(home, "~")
    return SyncInitResult(
        cwd=cwd,
        display_cwd=display_cwd,
        initial_reply_text=f"📓 Syncing memory notebook...\n(project: `{display_cwd}`)",
        parse_mode="Markdown",
    )


def run_sync_finish_use_case(sync_status: str, display_cwd: str) -> TextResult:
    status_map = {
        "synced": "📓 Memory notebook synced.",
        "sync script not found": "❌ Sync script not found.",
    }
    status_text = status_map.get(sync_status, f"⚠️ Sync: {sync_status}")
    return TextResult(
        reply_text=f"{status_text}\n(project: `{display_cwd}`)",
        parse_mode="Markdown",
    )


def run_history_resolve_use_case(
    *,
    topic_id: int,
    args: list[str],
    topic_all_sessions: dict[int, dict[str, str]],
    topic_active_session: dict[int, str],
    session_sdk_ids: dict[str, str],
) -> HistoryResolveResult:
    n = 5
    target_name = None

    if args:
        first = args[0]
        if first.isdigit():
            n = int(first)
            n = max(1, min(n, 50))
            if len(args) >= 2:
                target_name = args[1]
        else:
            target_name = first
            if len(args) >= 2 and args[1].isdigit():
                n = int(args[1])
                n = max(1, min(n, 50))

    sessions = topic_all_sessions.get(topic_id, {})
    if not sessions:
        return HistoryResolveResult(
            ok=False,
            reply_text="No sessions. Send a message to create one.",
            parse_mode=None,
        )

    if target_name:
        if target_name not in sessions:
            matches = [nm for nm in sessions if target_name.lower() in nm.lower()]
            if matches:
                hint = ", ".join(f"`{m}`" for m in matches)
                return HistoryResolveResult(
                    ok=False,
                    reply_text=f"Session `{target_name}` not found. Did you mean: {hint}?",
                    parse_mode="Markdown",
                )
            available = ", ".join(f"`{nm}`" for nm in sorted(sessions.keys()))
            return HistoryResolveResult(
                ok=False,
                reply_text=f"Session `{target_name}` not found.\nAvailable: {available}",
                parse_mode="Markdown",
            )
        session_name = target_name
        session_id = sessions[target_name]
    else:
        session_name = topic_active_session.get(topic_id)
        if not session_name or session_name not in sessions:
            return HistoryResolveResult(
                ok=False,
                reply_text="No active session.",
                parse_mode=None,
            )
        session_id = sessions[session_name]

    sdk_sid = session_sdk_ids.get(session_id)
    if not sdk_sid:
        return HistoryResolveResult(
            ok=False,
            reply_text=(
                f"Session `{session_name}` has no conversation history yet.\n"
                f"Send a message first to start a conversation."
            ),
            parse_mode="Markdown",
        )

    return HistoryResolveResult(
        ok=True,
        reply_text=None,
        parse_mode=None,
        n=n,
        session_name=session_name,
        session_id=session_id,
        sdk_sid=sdk_sid,
    )


def run_attach_session_use_case(
    *,
    topic_id: int,
    args: list[str],
    topic_all_sessions: dict[int, dict[str, str]],
    find_sdk_session: Callable[[str], Optional[tuple[str, str]]],
    check_active_terminal: Callable[[str], Optional[int]],
    validate_sdk_id: Callable[[str], bool],
    session_sdk_ids: dict[str, str],
) -> AttachSessionResult:
    """Attach an external Claude SDK session to the bot.

    Usage: /attach <sdk_session_id> <name>
    """
    if len(args) < 2:
        return AttachSessionResult(
            ok=False,
            reply_text=(
                "Usage: `/attach <sdk_session_id> <name>`\n\n"
                "Get SDK session IDs with:\n"
                "  `/session` — current session\n"
                "  `claude --resume` — in terminal (interactive picker)"
            ),
            parse_mode="Markdown",
        )

    sdk_session_id = args[0]
    name = args[1]

    # Validate UUID format
    if not validate_sdk_id(sdk_session_id):
        return AttachSessionResult(
            ok=False,
            reply_text=f"Invalid SDK session ID: `{sdk_session_id}`\nExpected UUID format.",
            parse_mode="Markdown",
        )

    # Check name not taken
    sessions = topic_all_sessions.get(topic_id, {})
    if name in sessions:
        return AttachSessionResult(
            ok=False,
            reply_text=f"Session name `{name}` already exists. Choose a different name.",
            parse_mode="Markdown",
        )

    # Check SDK session ID not already attached
    for existing_sid, existing_sdk in session_sdk_ids.items():
        if existing_sdk == sdk_session_id:
            # Find which session name has it
            for tid, sess in topic_all_sessions.items():
                for sname, ssid in sess.items():
                    if ssid == existing_sid:
                        return AttachSessionResult(
                            ok=False,
                            reply_text=(
                                f"SDK session `{sdk_session_id[:8]}...` is already "
                                f"attached to session `{sname}`."
                            ),
                            parse_mode="Markdown",
                        )
            break

    # Find the JSONL file and extract cwd
    lookup_result = find_sdk_session(sdk_session_id)
    if lookup_result is None:
        return AttachSessionResult(
            ok=False,
            reply_text=(
                f"SDK session `{sdk_session_id[:8]}...` not found.\n"
                f"No JSONL file found in `~/.claude/projects/`."
            ),
            parse_mode="Markdown",
        )

    _, cwd = lookup_result

    # Check for active terminal session
    active_pid = check_active_terminal(sdk_session_id)
    if active_pid is not None:
        return AttachSessionResult(
            ok=False,
            reply_text=(
                f"SDK session `{sdk_session_id[:8]}...` is currently "
                f"active in terminal (PID {active_pid}).\n"
                f"Close the terminal session first, then retry."
            ),
            parse_mode="Markdown",
        )

    # Success — return result for cmd_attach to execute side effects
    home = os.path.expanduser("~")
    cwd_display = cwd.replace(home, "~")

    return AttachSessionResult(
        ok=True,
        reply_text=(
            f"Attached session: `{name}`\n"
            f"SDK: `{sdk_session_id[:8]}...`\n"
            f"Project: `{cwd_display}` (locked)\n\n"
            f"To resume in terminal:\n"
            f"`cd {cwd_display} && claude --resume {sdk_session_id}`"
        ),
        parse_mode="Markdown",
        session_name=name,
        sdk_session_id=sdk_session_id,
        cwd=cwd,
    )


# ── Browse SDK sessions ─────────────────────────────────────────────────


def _format_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


def run_browse_use_case(
    *,
    args: list[str],
    list_project_dirs: Callable[[], list[tuple[str, int]]],
    list_sdk_sessions: Callable[..., list],
    session_sdk_ids: dict[str, str],
) -> TextResult:
    """Browse SDK session JSONL files on disk.

    Usage:
        /browse              — list project directories
        /browse <project>    — sessions in project (last 7 days)
        /browse <project> N  — sessions in project (last N days)
        /browse all          — all projects (last 7 days)
    """
    home = os.path.expanduser("~")

    # Determine filter and max_age_days from args
    project_filter: str | None = None
    max_age_days = 7

    if not args:
        # List project directories
        dirs = list_project_dirs()
        if not dirs:
            return TextResult(
                reply_text="No SDK session directories found.",
                parse_mode=None,
            )
        lines = ["📂 Project directories:\n"]
        for dirname, count in dirs:
            # Make the dirname more readable: strip leading dash, replace dashes
            display = dirname.replace("-home-gkh-", "~/").replace("-", "/")
            lines.append(f"  `{display}` ({count} sessions)")
        lines.append(
            f"\nTotal: {len(dirs)} projects\n"
            "\nUsage:\n"
            "  `/browse <keyword>` — sessions matching keyword (7 days)\n"
            "  `/browse <keyword> 30` — last 30 days\n"
            "  `/browse all` — all projects (7 days)"
        )
        return TextResult(reply_text="\n".join(lines), parse_mode="Markdown")

    # Parse args
    if len(args) >= 2:
        try:
            max_age_days = int(args[1])
        except ValueError:
            pass

    keyword = args[0]
    if keyword.lower() == "all":
        project_filter = None
    else:
        project_filter = keyword

    # Collect already-attached SDK IDs for marking
    attached_sdk_ids = set(session_sdk_ids.values())

    sessions = list_sdk_sessions(
        project_filter=project_filter,
        max_age_days=max_age_days,
    )

    if not sessions:
        label = f"matching `{keyword}`" if project_filter else "across all projects"
        return TextResult(
            reply_text=f"No sessions found {label} in the last {max_age_days} days.",
            parse_mode="Markdown",
        )

    # Group by project_dir
    by_project: dict[str, list] = {}
    for s in sessions:
        by_project.setdefault(s.project_dir, []).append(s)

    lines: list[str] = []
    total = 0
    for proj_dir, proj_sessions in by_project.items():
        display = proj_dir.replace("-home-gkh-", "~/").replace("-", "/")
        lines.append(f"📁 `{display}` ({len(proj_sessions)} sessions)\n")
        for s in proj_sessions:
            total += 1
            dt = datetime.fromtimestamp(s.mtime).strftime("%m-%d %H:%M")
            size = _format_size(s.size_bytes)
            attached = " ✅attached" if s.session_id in attached_sdk_ids else ""
            msg = s.first_message.replace("\n", " ").strip()
            if msg:
                msg = msg[:50]
            else:
                msg = "(empty)"
            lines.append(f"  `{dt}` {size}{attached}")
            lines.append(f"  `{s.session_id}`")
            lines.append(f"  _{msg}_\n")
        lines.append("")

    header = f"SDK Sessions (last {max_age_days}d): {total} found\n\n"
    footer = "→ `/attach <session_id> <name>` to attach"
    return TextResult(
        reply_text=header + "\n".join(lines) + footer,
        parse_mode="Markdown",
    )
