from __future__ import annotations

import pytest

from services.telegram_result_catalog import (
    TelegramResultDisposition,
    classify_telegram_rpc_error,
)


@pytest.mark.parametrize(
    ("name", "text", "code", "expected"),
    [
        ("UsernameNotOccupiedError", "", 400, "username_not_found"),
        ("ChannelPrivateError", "", 400, "channel_private"),
        ("InviteRequestSentError", "", 400, "join_requested"),
        ("ChatWriteForbiddenError", "", 403, "chat_write_forbidden"),
        ("ReactionInvalidError", "", 400, "reaction_invalid"),
        ("RandomIdDuplicateError", "", 400, "message_random_id_duplicate"),
        ("BadRequestError", "MESSAGE_TOO_LONG", 400, "message_too_long"),
        ("UnauthorizedError", "", 401, "authorization_required"),
        ("ForbiddenError", "", 403, "telegram_forbidden"),
        ("NotFoundError", "", 404, "telegram_not_found"),
        ("BadRequestError", "NEW_TELEGRAM_VALUE", 400, "telegram_bad_request"),
        ("RPCError", "SOMETHING_NEW", 499, "telegram_rpc_499"),
    ],
)
def test_deterministic_rpc_results(name, text, code, expected) -> None:
    result = classify_telegram_rpc_error(
        rpc_code=code,
        rpc_name=name,
        rpc_text=text,
        request_dispatched=True,
        retry_network=False,
    )
    assert result.disposition is TelegramResultDisposition.FAILED
    assert result.code == expected
    assert result.message


def test_transient_after_mutating_dispatch_is_uncertain() -> None:
    result = classify_telegram_rpc_error(
        rpc_code=500,
        rpc_name="ServerError",
        rpc_text="INTERNAL",
        request_dispatched=True,
        retry_network=False,
    )
    assert result.disposition is TelegramResultDisposition.UNCERTAIN
    assert result.code == "telegram_confirmation_lost_after_dispatch"


def test_transient_before_dispatch_is_deferred() -> None:
    result = classify_telegram_rpc_error(
        rpc_code=500,
        rpc_name="ServerError",
        rpc_text="INTERNAL",
        request_dispatched=False,
        retry_network=False,
    )
    assert result.disposition is TelegramResultDisposition.DEFERRED
    assert result.retry_after


def test_wait_seconds_are_preserved() -> None:
    result = classify_telegram_rpc_error(
        rpc_code=420,
        rpc_name="FloodWaitError",
        rpc_text="FLOOD_WAIT_123",
        request_dispatched=False,
        retry_network=True,
    )
    assert result.disposition is TelegramResultDisposition.DEFERRED
    assert result.code == "flood_wait_deferred"
    assert result.retry_after == 123
