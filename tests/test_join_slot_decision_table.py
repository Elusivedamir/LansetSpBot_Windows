from __future__ import annotations

import pytest

from workers.handlers.join_slot_decisions import (
    CampaignDisposition,
    CancellationDisposition,
    JoinErrorDisposition,
    JoinSlotPhase,
    LocalBanDisposition,
    campaign_disposition,
    cancellation_disposition,
    join_error_disposition,
    local_ban_disposition,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("running", CampaignDisposition.RUN),
        ("paused", CampaignDisposition.DEFER),
        ("network_wait", CampaignDisposition.DEFER),
        ("stopped", CampaignDisposition.CANCEL),
        ("completed", CampaignDisposition.CANCEL),
        ("", CampaignDisposition.CANCEL),
    ],
)
def test_campaign_disposition_table(status, expected) -> None:
    assert campaign_disposition(status) is expected


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (JoinSlotPhase.PRECHECK, CancellationDisposition.DEFER_BEFORE_DISPATCH),
        (JoinSlotPhase.READY_TO_JOIN, CancellationDisposition.DEFER_BEFORE_DISPATCH),
        (JoinSlotPhase.JOIN_STARTED, CancellationDisposition.UNCERTAIN_AFTER_DISPATCH),
        (JoinSlotPhase.JOIN_CONFIRMED, CancellationDisposition.UNCERTAIN_AFTER_DISPATCH),
    ],
)
def test_cancellation_boundary_table(phase, expected) -> None:
    assert cancellation_disposition(phase) is expected


@pytest.mark.parametrize(
    ("restricted", "strict", "current", "expected_account", "expected"),
    [
        (True, True, 7, 7, LocalBanDisposition.ACCOUNT_RESTRICTED),
        (False, True, 8, 7, LocalBanDisposition.ACCOUNT_MISMATCH),
        (False, False, 8, 7, LocalBanDisposition.TARGET_BLOCKED),
        (False, True, 7, 7, LocalBanDisposition.TARGET_BLOCKED),
    ],
)
def test_local_ban_disposition_table(
    restricted, strict, current, expected_account, expected
) -> None:
    assert local_ban_disposition(
        account_restricted=restricted,
        strict_account_binding=strict,
        current_account_id=current,
        expected_account_id=expected_account,
    ) is expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("network_unavailable", JoinErrorDisposition.NETWORK_WAIT),
        ("join_requested", JoinErrorDisposition.JOIN_REQUESTED),
        ("account_state_mismatch", JoinErrorDisposition.ACCOUNT_MISMATCH),
        ("join_result_unknown", JoinErrorDisposition.RESULT_UNKNOWN),
        ("peer_flood", JoinErrorDisposition.ACCOUNT_RESTRICTION),
        ("security_time_sync", JoinErrorDisposition.PAUSE_CAMPAIGN),
        ("permission_denied", JoinErrorDisposition.FAILED),
    ],
)
def test_join_error_disposition_table(code, expected) -> None:
    assert join_error_disposition(
        code,
        restriction_codes={"peer_flood", "user_restricted"},
    ) is expected
