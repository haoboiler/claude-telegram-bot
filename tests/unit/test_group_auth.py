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


def _group_update(user_id: int, *, text: str = "hello", is_topic: bool = False):
    return _Obj(
        effective_chat=_Obj(type="supergroup"),
        effective_user=_Obj(id=user_id),
        message=_Obj(
            text=text,
            entities=[],
            reply_to_message=None,
            is_topic_message=is_topic,
            message_thread_id=7 if is_topic else None,
        ),
        effective_message=None,
    )


class TestGroupAuthModeAllowed:
    """TELEGRAM_GROUP_AUTH_MODE=allowed — 白名单成员在群里放行 (多用户群 bot)."""

    def _call(self, update, mode, owner=100, allowed=frozenset({200, 300})):
        return should_respond_in_group(
            update,
            is_command=False,
            owner_user_id=owner,
            allowed_user_ids=set(allowed),
            bot_username="bot",
            logger=None,
            group_auth_mode=mode,
        )

    def test_owner_mode_blocks_allowed_user_when_owner_exists(self):
        # 原语义回归: owner 存在时白名单用户在群里被拒
        assert self._call(_group_update(200, is_topic=True), "owner") is False

    def test_allowed_mode_passes_allowed_user_in_topic(self):
        assert self._call(_group_update(200, is_topic=True), "allowed") is True

    def test_allowed_mode_passes_owner_in_topic(self):
        assert self._call(_group_update(100, is_topic=True), "allowed") is True

    def test_allowed_mode_blocks_stranger(self):
        assert self._call(_group_update(999, is_topic=True), "allowed") is False

    def test_allowed_mode_still_requires_topic_or_mention(self):
        # 身份放行不代表消息定向放行: 普通群消息无 @ 无 topic 仍被忽略
        assert self._call(_group_update(200, is_topic=False), "allowed") is False

    def test_allowed_mode_mention_passes(self):
        assert self._call(
            _group_update(200, text="@bot 帮我回测"), "allowed") is True

    def test_default_mode_unchanged(self):
        # 不传 group_auth_mode -> 默认 owner 语义
        update = _group_update(200, is_topic=True)
        assert should_respond_in_group(
            update, is_command=False, owner_user_id=100,
            allowed_user_ids={200}, bot_username="bot", logger=None,
        ) is False
