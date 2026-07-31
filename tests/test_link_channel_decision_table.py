from __future__ import annotations

import pytest

from workers.handlers.link_channel_decisions import (
    DeferredLinkDisposition,
    LinkErrorDisposition,
    deferred_link_disposition,
    group_link_status,
    link_error_disposition,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("shutdown_before_dispatch", DeferredLinkDisposition.PAUSE),
        ("local_ban_before_dispatch", DeferredLinkDisposition.LOCAL_BAN),
        ("flood_wait_deferred", DeferredLinkDisposition.SKIP_TARGET),
        ("slow_mode_wait_deferred", DeferredLinkDisposition.SKIP_TARGET),
    ],
)
def test_deferred_link_table(code, expected) -> None:
    assert deferred_link_disposition(code) is expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("join_result_unknown", LinkErrorDisposition.UNKNOWN_BAN),
        ("join_requested", LinkErrorDisposition.JOIN_REQUESTED),
        ("peer_flood", LinkErrorDisposition.RAISE_RESTRICTION),
        ("security_time_sync", LinkErrorDisposition.RAISE_RESTRICTION),
        ("permission_denied", LinkErrorDisposition.STORE_UNAVAILABLE),
    ],
)
def test_link_error_table(code, expected) -> None:
    assert link_error_disposition(code) is expected


def test_group_status_table() -> None:
    assert group_link_status(True).startswith("Связанное обсуждение")
    assert group_link_status(False).startswith("Группа")
