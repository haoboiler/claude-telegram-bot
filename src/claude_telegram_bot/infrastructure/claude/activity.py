from typing import Optional

from claude_agent_sdk import AssistantMessage, ToolUseBlock, TextBlock


# Tool name -> user-friendly description
TOOL_LABELS = {
    "Read": "Reading",
    "Edit": "Editing",
    "Write": "Writing",
    "Bash": "Running command",
    "Grep": "Searching",
    "Glob": "Finding files",
    "Task": "Running sub-agent",
    "Agent": "Running sub-agent",
    "WebFetch": "Fetching web page",
    "WebSearch": "Searching web",
    "TodoWrite": "Updating tasks",
    "AskUserQuestion": "Asking user",
}


def extract_activity(msg) -> Optional[str]:
    """Extract user-friendly activity description from an SDK message."""
    if not isinstance(msg, AssistantMessage):
        return None

    for block in msg.content:
        if isinstance(block, ToolUseBlock):
            tool_name = block.name
            label = TOOL_LABELS.get(tool_name, tool_name)
            inp = block.input or {}
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
            elif tool_name in ("Task", "Agent"):
                desc = inp.get("description", "")[:30]
                return f"{label}: {desc}" if desc else label
            return label
        elif isinstance(block, TextBlock):
            text = block.text
            if text:
                return "Thinking..."

    return None


def format_tool_use_from_dict(name: str, inp: dict) -> str:
    """Format a tool_use block from JSONL dict into a human-readable summary."""
    label = TOOL_LABELS.get(name, name)
    if name in ("Read", "Edit", "Write") and "file_path" in inp:
        short = inp["file_path"].split("/")[-1]
        return f"{label} {short}"
    elif name == "Bash" and "command" in inp:
        cmd = inp["command"][:60]
        return f"{label}: {cmd}"
    elif name == "Grep" and "pattern" in inp:
        return f"{label} '{inp['pattern'][:40]}'"
    elif name == "Glob" and "pattern" in inp:
        return f"{label} {inp['pattern'][:40]}"
    elif name in ("Task", "Agent"):
        desc = inp.get("description", "")[:40]
        return f"{label}: {desc}" if desc else label
    return label
