from typing import Optional

from claude_agent_sdk import AssistantMessage, TextBlock


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


def summarize_narration(text: str, limit: int = 280) -> str:
    """Collapse a multi-line narration into a single trimmed line for the activity log."""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + "…"
    return collapsed


def extract_activity(msg) -> Optional[str]:
    """Extract the live activity line from an SDK message.

    Only Claude's narration text (its running commentary — findings,
    decisions, next steps) is surfaced. Tool calls (Reading/Running
    command/etc.) are intentionally ignored: they were noise that drowned
    out the genuinely useful narration. Returns None for tool-only messages.
    """
    if not isinstance(msg, AssistantMessage):
        return None

    for block in msg.content:
        if isinstance(block, TextBlock):
            text = (block.text or "").strip()
            if text:
                return summarize_narration(text)

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
