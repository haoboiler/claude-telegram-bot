from __future__ import annotations


def parse_ask_callback_data(data: str) -> tuple[str, str] | None:
    """Parse ask callback_data like 'ask:<qid>:<choice>'."""
    if not data or not data.startswith("ask:"):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    _, qid, choice = parts
    return qid, choice


def resolve_ask_selection(choice: str, options: list[dict]) -> str:
    """Resolve callback choice to selected label (or raw choice fallback)."""
    try:
        option_idx = int(choice)
        if 0 <= option_idx < len(options):
            return options[option_idx]["label"]
        return choice
    except ValueError:
        return choice
