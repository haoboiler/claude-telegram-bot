"""Tests for domain/policies/group_auth.py."""

from __future__ import annotations

import pytest

from src.claude_telegram_bot.domain.policies.group_auth import (
    is_group_chat,
    should_respond_in_group,
)


class _Obj:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestIsGroupChat:
    def test_private_is_not_group(self):
        update = _Obj(effective_chat=_Obj(type="private"))
        assert is_group_chat(update) is False

    def test_supergroup_is_group(self):
        update = _Obj(effective_chat=_Obj(type="supergroup"))
        assert is_group_chat(update) is True

    def test_group_is_group(self):
        update = _Obj(effective_chat=_Obj(type="group"))
        assert is_group_chat(update) is True


class TestShouldRespondInGroup:
    def test_owner_command_allowed(self):
        update = _Obj(
            effective_chat=_Obj(type="supergroup"),
            effective_user=_Obj(id=100),
            message=_Obj(
                text="/start",
                entities=[],
                reply_to_message=None,
                is_topic_message=False,
                message_thread_id=None,
            ),
            effective_message=None,
        )
        result = should_respond_in_group(
            update,
            is_command=True,
            owner_user_id=100,
            allowed_user_ids=set(),
            bot_username="bot",
            logger=None,
        )
        assert result is True
