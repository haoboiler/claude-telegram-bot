from __future__ import annotations


def is_group_chat(update) -> bool:
    """Check if the message is from a group/supergroup chat."""
    if not update.effective_chat:
        return False
    return update.effective_chat.type in ("group", "supergroup")


def should_respond_in_group(
    update,
    *,
    is_command: bool,
    owner_user_id: int | None,
    allowed_user_ids: set[int],
    bot_username: str,
    logger=None,
    group_auth_mode: str = "owner",
) -> bool:
    """Determine if the bot should respond to this message in a group chat.

    group_auth_mode:
        "owner" (default) — original semantics: when an owner exists, only the
            owner may talk in groups; allowed_user_ids only applies ownerless.
        "allowed" — any user in allowed_user_ids (or the owner) may talk in
            groups. For multi-user group bots (e.g. trader interview groups).
    """
    if not is_group_chat(update):
        return True  # Private chat

    user_id = update.effective_user.id

    if group_auth_mode == "allowed":
        if user_id != owner_user_id and user_id not in allowed_user_ids:
            return False
    elif owner_user_id:
        if user_id != owner_user_id:
            return False
    elif allowed_user_ids:
        if user_id not in allowed_user_ids:
            return False
    else:
        return False

    if is_command:
        return True

    msg = update.message or update.effective_message
    if not msg:
        return False

    if getattr(msg, "is_topic_message", False) and msg.message_thread_id:
        return True

    if msg.text and bot_username:
        if f"@{bot_username}" in msg.text:
            return True

    if msg.entities and bot_username:
        for entity in msg.entities:
            if entity.type == "mention":
                mention_text = msg.text[entity.offset:entity.offset + entity.length]
                if mention_text.lower() == f"@{bot_username}".lower():
                    return True

    if msg.reply_to_message and msg.reply_to_message.from_user:
        reply_username = msg.reply_to_message.from_user.username
        if reply_username and bot_username:
            if reply_username.lower() == bot_username.lower():
                return True

    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.is_bot:
            if msg.text and bot_username and f"@{bot_username}" in msg.text:
                return True

    if logger:
        logger.debug(
            "Group message from owner ignored (no @mention or reply): "
            f"{msg.text[:50] if msg.text else '(empty)'}..."
        )
    return False
