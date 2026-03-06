import json
from pathlib import Path
from typing import Optional


def load_state(state_file: Path) -> dict:
    """Load persisted bot state (owner_id, allowed_users) from YAML file."""
    try:
        import yaml
        with open(state_file, "r") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, ImportError):
        return {}


def save_state(
    state_file: Path,
    owner_id: Optional[int],
    allowed_ids: set[int],
    logger=None,
) -> None:
    """Persist bot state to YAML file (fallback to JSON if PyYAML unavailable)."""
    try:
        import yaml
        data = {
            "owner_user_id": owner_id,
            "allowed_user_ids": sorted(allowed_ids) if allowed_ids else [],
        }
        with open(state_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        if logger:
            logger.info(f"Bot state saved to {state_file}")
    except ImportError:
        data = {
            "owner_user_id": owner_id,
            "allowed_user_ids": sorted(allowed_ids) if allowed_ids else [],
        }
        json_path = state_file.with_suffix(".json")
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2)
        if logger:
            logger.info(f"Bot state saved to {json_path} (yaml unavailable)")
    except Exception as e:
        if logger:
            logger.warning(f"Failed to save bot state: {e}")
