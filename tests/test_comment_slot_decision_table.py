from __future__ import annotations

import pytest

from workers.comment_slot.decisions import (
    DeferredCommentDisposition,
    deferred_comment_disposition,
    generated_draft_terminal_status,
    network_backoff_seconds,
    nonretryable_comment_decision,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("local_quiet_hours", DeferredCommentDisposition.QUIET_HOURS),
        ("local_ban_before_dispatch", DeferredCommentDisposition.LOCAL_BAN),
        ("shutdown_before_dispatch", DeferredCommentDisposition.SHUTDOWN),
        ("flood_wait_deferred", DeferredCommentDisposition.NETWORK_WAIT),
    ],
)
def test_deferred_comment_table(code: str, expected: DeferredCommentDisposition) -> None:
    assert deferred_comment_disposition(code) is expected


@pytest.mark.parametrize(
    ("code", "status", "consume", "pause"),
    [
        ("delivery_result_unknown", "uncertain", True, True),
        ("join_result_unknown", "uncertain", True, True),
        ("delivery_persist_failed", "uncertain", True, True),
        ("direct_message_persist_failed", "uncertain", True, True),
        ("chat_write_forbidden", "skipped", True, False),
        ("channel_private", "skipped", True, False),
        ("account_state_mismatch", "failed", False, True),
    ],
)
def test_nonretryable_comment_table(
    code: str, status: str, consume: bool, pause: bool
) -> None:
    decision = nonretryable_comment_decision(code, "fallback")
    assert decision.final_status == status
    assert decision.consume_channel is consume
    assert decision.pause_campaign is pause


def test_network_backoff_is_bounded() -> None:
    assert [network_backoff_seconds(value) for value in (1, 2, 3, 6, 99)] == [
        60,
        180,
        300,
        1800,
        1800,
    ]


@pytest.mark.parametrize(
    ("current", "sent", "started", "deferred", "expected"),
    [
        ("generated", True, True, False, "sent"),
        ("sending", False, True, False, "uncertain"),
        ("generated", False, False, True, "cancelled"),
        ("generated", False, False, False, "failed"),
        ("generation_failed", False, False, False, "generation_failed"),
    ],
)
def test_generated_draft_terminal_table(
    current: str,
    sent: bool,
    started: bool,
    deferred: bool,
    expected: str,
) -> None:
    assert generated_draft_terminal_status(
        current_status=current,
        sent=sent,
        send_started=started,
        slot_deferred=deferred,
    ) == expected
