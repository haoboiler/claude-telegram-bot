import json
import os

from .activity import format_tool_use_from_dict


def derive_project_slug(cwd: str) -> str:
    """Derive the Claude CLI project slug from a working directory path."""
    cwd = os.path.realpath(cwd)
    return cwd.replace("/", "-").replace("_", "-")


def read_session_history(cwd: str, sdk_session_id: str, n: int = 5) -> list[dict]:
    """Read the last N conversation turns from a Claude CLI session JSONL file."""
    slug = derive_project_slug(cwd)
    jsonl_path = os.path.expanduser(
        f"~/.claude/projects/{slug}/{sdk_session_id}.jsonl"
    )

    if not os.path.isfile(jsonl_path):
        raise FileNotFoundError(jsonl_path)

    turns: list[dict] = []

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            rec_type = record.get("type")

            if rec_type == "user":
                msg = record.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str):
                    turns.append({"role": "user", "text": content})
                else:
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "tool_result":
                                parts.append("[tool result]")
                            elif block.get("type") == "text":
                                parts.append(block.get("text", ""))
                    turns.append(
                        {
                            "role": "user",
                            "text": " ".join(parts) if parts else "[structured input]",
                        }
                    )

            elif rec_type == "assistant":
                msg = record.get("message", {})
                content_blocks = msg.get("content", [])
                formatted_blocks = []

                for block in content_blocks:
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text:
                            formatted_blocks.append(("text", text))
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = block.get("input", {})
                        summary = format_tool_use_from_dict(name, inp)
                        formatted_blocks.append(("tool", summary))
                    # Skip "thinking" blocks

                if formatted_blocks:
                    turns.append({"role": "assistant", "blocks": formatted_blocks})

    if not turns:
        raise ValueError("No conversation data found in session file.")

    # Collect last N assistant turns with their preceding user messages
    result = []
    assistant_count = 0

    for i in range(len(turns) - 1, -1, -1):
        if turns[i]["role"] == "assistant":
            assistant_count += 1
            if assistant_count > n:
                break
            if i > 0 and turns[i - 1]["role"] == "user":
                result.append(turns[i - 1])
            result.append(turns[i])

    result.reverse()
    return result


def format_history_message(turns: list[dict], session_name: str, n: int) -> str:
    """Format history turns into a Telegram-friendly plain-text message."""
    lines = [f"📜 History [{session_name}] (last {n} assistant messages):\n"]

    for turn in turns:
        if turn["role"] == "user":
            text = turn.get("text", "")
            if text in ("[tool result]", "[structured input]"):
                continue
            if len(text) > 200:
                text = text[:200] + "..."
            lines.append(f">> {text}\n")

        elif turn["role"] == "assistant":
            blocks = turn.get("blocks", [])
            for btype, content in blocks:
                if btype == "text":
                    if len(content) > 500:
                        content = content[:500] + "..."
                    lines.append(content)
                elif btype == "tool":
                    lines.append(f"  🔧 {content}")
            lines.append("")

    return "\n".join(lines)
