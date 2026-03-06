"""Tests for bootstrap/auth_state.py — auth precedence logic."""

from __future__ import annotations

import pytest

from src.claude_telegram_bot.bootstrap.auth_state import (
    parse_allowed_user_ids,
    resolve_auth_bootstrap_state,
)


class TestParseAllowedUserIds:
    def test_csv_parse(self):
        assert parse_allowed_user_ids("1,2,3") == {1, 2, 3}

    def test_empty_string(self):
        result = parse_allowed_user_ids("")
        assert result == set() or result is None

    def test_single_id(self):
        assert parse_allowed_user_ids("42") == {42}

    def test_whitespace_tolerant(self):
        result = parse_allowed_user_ids("1, 2 , 3")
        assert 1 in result and 2 in result and 3 in result


class TestResolveAuthBootstrapState:
    def test_env_owner_takes_priority(self):
        result = resolve_auth_bootstrap_state(
            owner_env_value="9",
            allowed_env_value="1,2",
            persisted_state={"owner_user_id": 7, "allowed_user_ids": [7]},
        )
        assert result.owner_user_id == 9
        assert result.allowed_user_ids == {1, 2}
        assert result.loaded_owner_from_state is False
        assert result.loaded_allowed_from_state is False

    def test_owner_derived_from_allowed_env(self):
        result = resolve_auth_bootstrap_state(
            owner_env_value="",
            allowed_env_value="11,12",
            persisted_state={},
        )
        assert result.owner_user_id in {11, 12}
        assert result.allowed_user_ids == {11, 12}

    def test_state_fallback(self):
        result = resolve_auth_bootstrap_state(
            owner_env_value="",
            allowed_env_value="",
            persisted_state={"owner_user_id": 5, "allowed_user_ids": [5, 6]},
        )
        assert result.owner_user_id == 5
        assert result.allowed_user_ids == {5, 6}
        assert result.loaded_owner_from_state is True
        assert result.loaded_allowed_from_state is True

    def test_empty_everything(self):
        result = resolve_auth_bootstrap_state(
            owner_env_value="",
            allowed_env_value="",
            persisted_state={},
        )
        assert result.owner_user_id is None
        assert result.allowed_user_ids == set()
