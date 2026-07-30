from __future__ import annotations

import pytest

from core.campaign_state import (
    CampaignStatus,
    InvalidCampaignTransition,
    allowed_campaign_transitions,
    can_transition_campaign,
    is_terminal_campaign_status,
    require_campaign_transition,
    validate_campaign_path,
)


def test_happy_path_can_complete() -> None:
    path = validate_campaign_path(
        [
            "draft",
            "planned",
            "running",
            "paused_floodwait",
            "running",
            "completed",
        ]
    )

    assert path == (
        CampaignStatus.DRAFT,
        CampaignStatus.PLANNED,
        CampaignStatus.RUNNING,
        CampaignStatus.PAUSED_FLOODWAIT,
        CampaignStatus.RUNNING,
        CampaignStatus.COMPLETED,
    )


@pytest.mark.parametrize(
    "status",
    [
        CampaignStatus.COMPLETED,
        CampaignStatus.CANCELLED,
        CampaignStatus.FAILED,
    ],
)
def test_terminal_states_have_no_outgoing_transitions(
    status: CampaignStatus,
) -> None:
    assert is_terminal_campaign_status(status)
    assert allowed_campaign_transitions(status) == frozenset()


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "running"),
        ("planned", "completed"),
        ("completed", "running"),
        ("cancelled", "planned"),
        ("failed", "running"),
    ],
)
def test_invalid_transitions_fail_closed(current: str, target: str) -> None:
    assert not can_transition_campaign(current, target)

    with pytest.raises(InvalidCampaignTransition):
        require_campaign_transition(current, target)


def test_idempotent_transition_is_allowed_by_default() -> None:
    assert can_transition_campaign("running", "running")
    assert require_campaign_transition("running", "running") == (
        CampaignStatus.RUNNING,
        CampaignStatus.RUNNING,
    )


def test_idempotent_transition_can_be_rejected_for_compare_and_set_callers() -> None:
    assert not can_transition_campaign(
        "running",
        "running",
        allow_idempotent=False,
    )

    with pytest.raises(InvalidCampaignTransition):
        require_campaign_transition(
            "running",
            "running",
            allow_idempotent=False,
        )


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown campaign status"):
        require_campaign_transition("running", "teleported")
