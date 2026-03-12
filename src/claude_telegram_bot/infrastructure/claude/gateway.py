from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    TextBlock,
)
from telegram.constants import ChatAction

from .activity import extract_activity


@dataclass
class ClaudeGatewayConfig:
    max_turns: int
    timeout_seconds: int
    heartbeat_interval: int
    stall_warn_timeout: int


@dataclass
class UsageInfo:
    """Token usage and cost information from a Claude call."""
    num_turns: int = 0
    duration_ms: int = 0
    total_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def summary_line(self) -> str:
        """Render a compact one-line summary for appending to bot replies."""
        parts: list[str] = []
        if self.num_turns:
            parts.append(f"Turns: {self.num_turns}")
        total_tokens = self.input_tokens + self.output_tokens
        if total_tokens:
            parts.append(f"Tokens: {_fmt_k(total_tokens)}")
        if self.total_cost_usd is not None:
            parts.append(f"Cost: ${self.total_cost_usd:.4f}")
        if self.duration_ms:
            secs = self.duration_ms / 1000
            if secs >= 60:
                mins, secs_rem = divmod(int(secs), 60)
                parts.append(f"Time: {mins}m{secs_rem:02d}s")
            else:
                parts.append(f"Time: {secs:.0f}s")
        return " | ".join(parts) if parts else ""


def _fmt_k(n: int) -> str:
    """Format token count: 1234 -> '1.2k', 56 -> '56'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class ClaudeGateway:
    """SDK integration boundary for Claude calls."""

    def __init__(
        self,
        config: ClaudeGatewayConfig,
        session_sdk_ids: dict[str, str],
        session_clients: dict[str, object],
        get_session_cwd: Callable[[str], str],
        make_can_use_tool: Callable,
        update_thinking_msg: Callable,
        thread_id_or_none: Callable[[int], Optional[int]],
        logger,
    ) -> None:
        self.config = config
        self.session_sdk_ids = session_sdk_ids
        self.session_clients = session_clients
        self.get_session_cwd = get_session_cwd
        self.make_can_use_tool = make_can_use_tool
        self.update_thinking_msg = update_thinking_msg
        self.thread_id_or_none = thread_id_or_none
        self.log = logger

    async def call(
        self,
        prompt: str,
        session_id: str,
        thinking_msg=None,
        chat=None,
        session_name: str = "",
        topic_id: int = 0,
        _retry_depth: int = 0,
    ) -> tuple[str, list[str], UsageInfo]:
        if _retry_depth > 1:
            self.log.error(
                f"Max retry depth exceeded for session {session_id[:8]} "
                f"(depth={_retry_depth})"
            )
            return "Error: Max retry depth exceeded after resume failure.", [], UsageInfo()
        effective_cwd = self.get_session_cwd(session_id)
        options = ClaudeAgentOptions(
            cwd=effective_cwd,
            max_turns=self.config.max_turns,
            permission_mode="bypassPermissions",
            can_use_tool=(
                self.make_can_use_tool(chat, session_name, topic_id) if chat else None
            ),
            setting_sources=["user", "project"],
            max_buffer_size=3_145_728,  # 3MB – prevent SDK JSON decode errors on large responses
        )

        sdk_sid = self.session_sdk_ids.get(session_id)
        if sdk_sid:
            options.resume = sdk_sid

        self.log.info(
            f"Calling Claude SDK for session {session_id[:8]}... "
            f"(resume={'yes' if sdk_sid else 'no'}, cwd={effective_cwd})"
        )

        client = ClaudeSDKClient(options=options)
        self.session_clients[session_id] = client

        try:
            await client.connect()
            await client.query(prompt)

            activity_log: list[str] = []
            result_text = ""
            last_assistant_text = ""
            actual_session_id = None
            usage_info = UsageInfo()
            start_time = time.time()
            last_edit_time = 0.0
            last_heartbeat_time = start_time
            edit_throttle = 3.0

            async for msg in client.receive_response():
                now = time.time()
                elapsed = now - start_time

                if self.config.timeout_seconds > 0 and elapsed >= self.config.timeout_seconds:
                    self.log.error(f"Claude SDK timed out after {int(elapsed)}s")
                    try:
                        await client.interrupt()
                    except Exception:
                        pass
                    return (
                        f"[Timeout] Total timeout ({self.config.timeout_seconds}s) exceeded. "
                        f"Use /kill to stop earlier.",
                        activity_log,
                        usage_info,
                    )

                activity = extract_activity(msg)
                if activity and (not activity_log or activity_log[-1] != activity):
                    activity_log.append(activity)

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            last_assistant_text = block.text

                if thinking_msg and activity and (now - last_edit_time) >= edit_throttle:
                    last_edit_time = now
                    await self.update_thinking_msg(
                        thinking_msg, activity_log, session_name, elapsed
                    )

                if chat and (now - last_heartbeat_time) >= self.config.heartbeat_interval:
                    last_heartbeat_time = now
                    try:
                        await chat.send_action(
                            ChatAction.TYPING,
                            message_thread_id=self.thread_id_or_none(topic_id),
                        )
                    except Exception:
                        pass

                    if not activity_log or (now - last_edit_time) >= self.config.stall_warn_timeout:
                        if thinking_msg:
                            mins, secs = divmod(int(elapsed), 60)
                            time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                            label = f"[{session_name}] " if session_name else ""
                            last_act = activity_log[-1] if activity_log else "Starting..."
                            try:
                                await thinking_msg.edit_text(
                                    f"{label}Long thinking... ({time_str})\n"
                                    f"Last: {last_act}\n"
                                    f"Use /kill {session_name} if stuck"
                                )
                            except Exception:
                                pass

                if isinstance(msg, ResultMessage):
                    actual_session_id = msg.session_id
                    result_text = msg.result or ""
                    if msg.is_error:
                        result_text = f"[Error] {result_text}"
                    if msg.subtype == "error_max_turns":
                        result_text += (
                            f"\n\n[Reached max turns ({msg.num_turns}). "
                            f"Task may be incomplete.]"
                        )
                    # Capture usage info from ResultMessage
                    usage_dict = msg.usage or {}
                    usage_info = UsageInfo(
                        num_turns=msg.num_turns,
                        duration_ms=msg.duration_ms,
                        total_cost_usd=msg.total_cost_usd,
                        input_tokens=usage_dict.get("input_tokens", 0),
                        output_tokens=usage_dict.get("output_tokens", 0),
                    )

            if actual_session_id:
                self.session_sdk_ids[session_id] = actual_session_id

            return (
                result_text
                or last_assistant_text
                or "[Task completed but no summary was produced.]",
                activity_log,
                usage_info,
            )

        except CLINotFoundError:
            return "Error: claude CLI not found. Make sure it's installed.", [], UsageInfo()
        except ProcessError as e:
            error_msg = str(e)
            self.log.error(
                f"ProcessError for session {session_id[:8]}: {error_msg} "
                f"(was_resume={bool(sdk_sid)})"
            )

            if sdk_sid:
                self.log.warning(
                    f"Resume failed for {session_id[:8]}, clearing sdk_sid "
                    f"and retrying without resume (retry_depth={_retry_depth})"
                )
                self.session_sdk_ids.pop(session_id, None)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self.session_clients.pop(session_id, None)
                return await self.call(
                    prompt,
                    session_id,
                    thinking_msg=thinking_msg,
                    chat=chat,
                    session_name=session_name,
                    topic_id=topic_id,
                    _retry_depth=_retry_depth + 1,
                )

            if "already in use" in error_msg:
                return "Error: Session is already in use. Try /clear to reset.", [], UsageInfo()
            return f"Error: {error_msg}", [], UsageInfo()
        except Exception as e:
            self.log.exception("Unexpected error calling Claude SDK")
            return f"Error {type(e).__name__}: {e}", [], UsageInfo()
        finally:
            self.session_clients.pop(session_id, None)
            try:
                await client.disconnect()
            except Exception:
                pass
