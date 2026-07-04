[tgcc] 好的，直接给你完整方案。

## 核心思路：用 --output-format stream-json 替代 -p

### 当前 vs 改进

| | 当前 | 改进后 |
|---|---|---|
| CLI 参数 | claude -p "prompt" | claude -p "prompt" --output-format stream-json |
| 输出方式 | 等进程结束才有输出 | 每个步骤实时输出 JSON 事件 |
| 心跳内容 | Working... (5m32s) | Reading nile/config/settings.py... (5m32s) |
| 判断卡住 | 只能靠总超时 | 5分钟无新事件 = 可能卡住 |

### stream-json 事件格式示例


{"type":"assistant","message":{"content":[{"type":"text","text":"Let me look at..."}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/home/..."}}]}}
{"type":"tool_result","content":"file contents..."}
{"type":"result","result":"Final answer text here","duration_ms":45000}


### 改进后的 call_claude 实现

```python
import json

# Stall detection: no new events for this many seconds = likely stuck
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "300"))  # 5 min

# Tool name → user-friendly description
TOOL_LABELS = {
    "Read": "Reading",
    "Edit": "Editing",
    "Write": "Writing",
    "Bash": "Running command",
    "Grep": "Searching",
    "Glob": "Finding files",
    "Task": "Running sub-agent",
    "WebFetch": "Fetching web page",
    "WebSearch": "Searching web",
    "TodoWrite": "Updating tasks",
}


def _format_activity(event: dict) -> str | None:
    """Extract user-friendly activity description from a stream-json event."""
    if event.get("type") == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                label = TOOL_LABELS.get(tool_name, tool_name)
                # Extract short context from input
                inp = block.get("input", {})
                if tool_name in ("Read", "Edit", "Write") and "file_path" in inp:
                    path = inp["file_path"]
                    short = path.split("/")[-1]  # just filename
                    return f"{label} {short}"
                elif tool_name == "Bash" and "command" in inp:
                    cmd = inp["command"][:40]
                    return f"{label}: {cmd}"
                elif tool_name == "Grep" and "pattern" in inp:
                    return f"{label} '{inp['pattern'][:30]}'"
                elif tool_name == "Glob" and "pattern" in inp:
                    return f"{label} {inp['pattern'][:30]}"
                elif tool_name == "Task":
                    desc = inp.get("description", "")[:30]
                    return f"{label}: {desc}" if desc else label
                return label
            elif block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    return "Thinking..."
    return None


async def call_claude(prompt: str, session_id: str, is_new: bool = True,
                      thinking_msg=None, chat=None, session_name: str = "") -> str:
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--max-turns", "30",  # safety net
    ]

    if is_new:
        cmd += ["--session-id", session_id]
    else:
        cmd += ["--resume", session_id]

    log.info(f"Calling claude with session {session_id[:8]}...")

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORK_DIR,
            env=env,
        )

        start_time = asyncio.get_event_loop().time()
        last_event_time = start_time
        current_activity = "Starting..."
        final_result = None
        all_text_parts = []

        # Read stdout line-by-line (stream-json outputs one JSON per line)
        while True:
            now = asyncio.get_event_loop().time()
            elapsed = int(now - start_time)
            since_last_event = int(now - last_event_time)

            # Total timeout
            if elapsed >= CLAUDE_TIMEOUT:
                log.error(f"Claude CLI timed out after {elapsed}s")
                try:
                    proc.kill()
                except Exception:
                    pass
                return f"[Timeout] Total timeout ({CLAUDE_TIMEOUT}s) exceeded."

            # Stall detection
            if since_last_event >= STALL_TIMEOUT:
                log.error(f"Claude stalled - no events for {since_last_event}s")
                try:
                    proc.kill()
                except Exception:
                    pass
                return (
                    f"[Stall] No activity for {STALL_TIMEOUT}s, likely stuck.\n"
                    f"Last activity: {current_activity}\n"
                    f"Total elapsed: {elapsed}s"
                )

            # Try to read a line with heartbeat interval timeout
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=HEARTBEAT_INTERVAL
                )
            except asyncio.TimeoutError:
                # No new line - send heartbeat with current activity
                if thinking_msg:
                    mins, secs = divmod(elapsed, 60)
                    time_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                    label = f"[{session_name}] " if session_name else ""
                    try:
                        await thinking_msg.edit_text(
                            f"{label}{current_activity} ({time_str})"
                        )
                    except Exception:
                        pass
                if chat:
                    try:
                        await chat.send_action(ChatAction.TYPING)
                    except Exception:
                        pass
                continue

            if not line:
                # EOF - process ended
                break

            # Parse the JSON event
            last_event_time = asyncio.get_event_loop().time()
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue

            # Extract activity for display
            activity = _format_activity(event)
            if activity:
                current_activity = activity

            # Capture result
            if event.get("type") == "result":
                final_result = event.get("result", "")
            elif event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        all_text_parts.append(block["text"])

        # Wait for process to finish
        await proc.wait()

        # Return result
        if final_result:
            return final_result
        if all_text_parts:
            return "\n".join(all_text_parts)

        # Fallback: read any remaining stderr
        stderr = await proc.stderr.read()
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and err:
            return f"Error Claude CLI failed (code {proc.returncode}):\n{err}"

        return "No output from Claude"

    except FileNotFoundError:
        return "Error claude CLI not found. Make sure it's in PATH."
    except Exception as e:
        log.exception("Unexpected error calling claude")
        return f"Error {type(e).__name__}: {e}"
```

### 用户体验对比

**Before（当前）：**
```
s1 Working... (0s)
s1 Working... (30s)
s1 Working... (1m00s)
s1 Working... (5m00s)     ← 在做什么？还活着吗？
s1 Working... (10m00s)    ← 用户焦虑...
s1 Working... (29m30s)    ← ???
Timeout Claude did not respond within 1800s   ← 白等了30分钟
```

**After（改进后）：**
```
s1 Starting... (0s)
s1 Searching 'funding_rate'... (8s)
[s1] Reading publisher.py... (15s)
[s1] Reading account_snapshot.py... (28s)
[s1] Running sub-agent: Explore codebase... (1m05s)
[s1] Editing settings.py... (3m20s)
[s1] Running command: pytest tests/... (5m10s)
```

**如果卡住了：**
```
[s1] Reading publisher.py... (5m00s)
[s1] Reading publisher.py... (5m30s)    ← 同一活动持续太久
[Stall] No activity for 300s, likely stuck.
Last activity: Reading publisher.py
Total elapsed: 612s
```

### 关键改进点总结

| 改进 | 效果 |
|------|------|
| stream-json 实时事件 | 用户看到 Claude 正在做什么，而不只是时间 |
| Stall 检测 (5min无新事件) | 区分"正在工作"和"已经卡死" |
| `--max-turns 30` | 防止无限循环，间接限制时间 |
| 活动描述映射 | Read → `Reading settings.py`，用户友好 |
| 双层超时 | 总超时 1800s + Stall 超时 300s，两道安全网 |

要我直接把这个改动写入 telegram_bot.py 吗？
