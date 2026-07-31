from __future__ import annotations

import pytest

from workers.queue_task_decisions import (
    CancellationPersistence,
    cancellation_persistence,
    parse_account_identity,
    unexpected_retry_allowed,
)


@pytest.mark.parametrize(
    ("task", "column", "payload", "owner", "mismatch"),
    [
        ({"account_id": 7, "payload": {"account_id": 7}}, 7, 7, 7, False),
        ({"account_id": 7, "payload": {}}, 7, 0, 7, False),
        ({"payload": {"account_id": 8}}, 0, 8, 8, False),
        ({"account_id": 7, "payload": {"account_id": 8}}, 7, 8, 7, True),
        ({"account_id": "bad", "payload": {"account_id": 8}}, 0, 0, 0, False),
    ],
)
def test_account_identity_table(
    task: dict, column: int, payload: int, owner: int, mismatch: bool
) -> None:
    identity = parse_account_identity(task)
    assert identity.column_account_id == column
    assert identity.payload_account_id == payload
    assert identity.account_id == owner
    assert identity.mismatch is mismatch


def test_cancellation_persistence_never_requeues_mutating_task() -> None:
    idempotent = frozenset({"sync_channels"})
    assert cancellation_persistence("sync_channels", idempotent) is CancellationPersistence.REQUEUE
    assert cancellation_persistence("auto_comment_slot", idempotent) is CancellationPersistence.UNCERTAIN_FAILURE


def test_unexpected_retry_is_idempotent_and_bounded() -> None:
    idempotent = frozenset({"link_channels"})
    assert unexpected_retry_allowed(
        task_type="link_channels", retry_count=1, max_retries=3,
        idempotent_task_types=idempotent,
    )
    assert not unexpected_retry_allowed(
        task_type="link_channels", retry_count=3, max_retries=3,
        idempotent_task_types=idempotent,
    )
    assert not unexpected_retry_allowed(
        task_type="auto_comment_slot", retry_count=0, max_retries=3,
        idempotent_task_types=idempotent,
    )
