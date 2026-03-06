from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthBootstrapState:
    owner_user_id: int | None
    allowed_user_ids: set[int]
    loaded_owner_from_state: bool = False
    loaded_allowed_from_state: bool = False


def parse_allowed_user_ids(env_value: str) -> set[int]:
    """Parse TELEGRAM_ALLOWED_USERS env value."""
    if not env_value:
        return set()
    return {
        int(x.strip())
        for x in env_value.split(",")
        if x.strip()
    }


def resolve_auth_bootstrap_state(
    *,
    owner_env_value: str,
    allowed_env_value: str,
    persisted_state: dict,
) -> AuthBootstrapState:
    """Resolve owner/allowed users with stable precedence.

    Precedence (compatible with previous behavior):
    1. `TELEGRAM_OWNER_ID`
    2. derive owner from `TELEGRAM_ALLOWED_USERS`
    3. persisted state (only when both owner/allowed are absent)
    """
    allowed_user_ids = parse_allowed_user_ids(allowed_env_value)

    owner_user_id: int | None = None
    if owner_env_value:
        owner_user_id = int(owner_env_value.strip())
    elif allowed_user_ids:
        owner_user_id = next(iter(allowed_user_ids))

    loaded_owner_from_state = False
    loaded_allowed_from_state = False

    if not owner_user_id and not allowed_user_ids:
        raw_owner = persisted_state.get("owner_user_id")
        raw_allowed = persisted_state.get("allowed_user_ids")
        if raw_owner:
            owner_user_id = int(raw_owner)
            loaded_owner_from_state = True
        if raw_allowed:
            allowed_user_ids = {int(x) for x in raw_allowed}
            loaded_allowed_from_state = True

    return AuthBootstrapState(
        owner_user_id=owner_user_id,
        allowed_user_ids=allowed_user_ids,
        loaded_owner_from_state=loaded_owner_from_state,
        loaded_allowed_from_state=loaded_allowed_from_state,
    )
