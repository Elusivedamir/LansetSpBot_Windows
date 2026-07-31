"""Pure state tables for one persisted join campaign slot."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Collection


class JoinSlotPhase(IntEnum):
    PRECHECK = 1
    MEMBERSHIP_CHECKED = 2
    READY_TO_JOIN = 3
    JOIN_STARTED = 4
    JOIN_CONFIRMED = 5


class CampaignDisposition(StrEnum):
    RUN = "run"
    DEFER = "defer"
    CANCEL = "cancel"


class CancellationDisposition(StrEnum):
    DEFER_BEFORE_DISPATCH = "defer_before_dispatch"
    UNCERTAIN_AFTER_DISPATCH = "uncertain_after_dispatch"


class LocalBanDisposition(StrEnum):
    ACCOUNT_RESTRICTED = "account_restricted"
    ACCOUNT_MISMATCH = "account_mismatch"
    TARGET_BLOCKED = "target_blocked"


class JoinErrorDisposition(StrEnum):
    NETWORK_WAIT = "network_wait"
    JOIN_REQUESTED = "join_requested"
    ACCOUNT_MISMATCH = "account_mismatch"
    RESULT_UNKNOWN = "result_unknown"
    ACCOUNT_RESTRICTION = "account_restriction"
    PAUSE_CAMPAIGN = "pause_campaign"
    FAILED = "failed"


def campaign_disposition(status: str) -> CampaignDisposition:
    normalized = str(status or "")
    if normalized == "running":
        return CampaignDisposition.RUN
    if normalized in {"paused", "network_wait"}:
        return CampaignDisposition.DEFER
    return CampaignDisposition.CANCEL


def cancellation_disposition(phase: JoinSlotPhase) -> CancellationDisposition:
    if phase < JoinSlotPhase.JOIN_STARTED:
        return CancellationDisposition.DEFER_BEFORE_DISPATCH
    return CancellationDisposition.UNCERTAIN_AFTER_DISPATCH


def local_ban_disposition(
    *,
    account_restricted: bool,
    strict_account_binding: bool,
    current_account_id: int,
    expected_account_id: int,
) -> LocalBanDisposition:
    if account_restricted:
        return LocalBanDisposition.ACCOUNT_RESTRICTED
    if strict_account_binding and int(current_account_id) != int(expected_account_id):
        return LocalBanDisposition.ACCOUNT_MISMATCH
    return LocalBanDisposition.TARGET_BLOCKED


def join_error_disposition(
    code: str,
    *,
    restriction_codes: Collection[str],
) -> JoinErrorDisposition:
    normalized = str(code or "")
    if normalized == "network_unavailable":
        return JoinErrorDisposition.NETWORK_WAIT
    if normalized == "join_requested":
        return JoinErrorDisposition.JOIN_REQUESTED
    if normalized == "account_state_mismatch":
        return JoinErrorDisposition.ACCOUNT_MISMATCH
    if normalized == "join_result_unknown":
        return JoinErrorDisposition.RESULT_UNKNOWN
    if normalized in restriction_codes:
        return JoinErrorDisposition.ACCOUNT_RESTRICTION
    if normalized in {"flood_wait_long", "flood_wait_repeated", "security_time_sync"}:
        return JoinErrorDisposition.PAUSE_CAMPAIGN
    return JoinErrorDisposition.FAILED
