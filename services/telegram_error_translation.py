from __future__ import annotations

from telethon.errors import (
    ChatRestrictedError,
    ChatSendPlainForbiddenError,
    ChatWriteForbiddenError,
    EntityBoundsInvalidError,
    MessageTooLongError,
    UserBannedInChannelError,
    UserIsBlockedError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
    YouBlockedUserError,
)

from core.exceptions import NonRetryableTelegramError


PERMANENT_SEND_ERRORS = (
    UserNotParticipantError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    MessageTooLongError,
    EntityBoundsInvalidError,
    YouBlockedUserError,
    UserIsBlockedError,
    ChatSendPlainForbiddenError,
    ChatRestrictedError,
    UserPrivacyRestrictedError,
)


def translate_permanent_send_error(exc: Exception) -> NonRetryableTelegramError:
    """Map one Telegram send rejection to a precise stable application code."""
    if isinstance(exc, UserNotParticipantError):
        message, code = (
            "Telegram requires membership before this action",
            "join_required",
        )
    elif isinstance(exc, ChatWriteForbiddenError):
        message, code = (
            "Telegram rejected writing to this chat",
            "chat_write_forbidden",
        )
    elif isinstance(exc, UserBannedInChannelError):
        message, code = (
            "The account is banned from writing in this chat",
            "user_banned",
        )
    elif isinstance(exc, MessageTooLongError):
        message, code = "Telegram message is too long", "message_too_long"
    elif isinstance(exc, EntityBoundsInvalidError):
        message, code = (
            "Telegram message formatting entities are invalid",
            "entity_bounds_invalid",
        )
    elif isinstance(exc, (YouBlockedUserError, UserIsBlockedError)):
        message, code = "Telegram peer is blocked", "user_blocked"
    elif isinstance(exc, ChatSendPlainForbiddenError):
        message, code = (
            "Plain text messages are forbidden in this chat",
            "plain_text_forbidden",
        )
    elif isinstance(exc, ChatRestrictedError):
        message, code = "Telegram chat is restricted", "chat_restricted"
    elif isinstance(exc, UserPrivacyRestrictedError):
        message, code = (
            "Telegram privacy settings reject this action",
            "privacy_restricted",
        )
    else:  # pragma: no cover - guarded by PERMANENT_SEND_ERRORS
        message, code = "Telegram rejected this send", "telegram_send_rejected"
    return NonRetryableTelegramError(
        message,
        code=code,
        details={
            "rpc_error": type(exc).__name__,
            "rpc_message": str(exc),
        },
    )
