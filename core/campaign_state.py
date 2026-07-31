from __future__ import annotations



from enum import StrEnum
from types import MappingProxyType
from typing import Final, Iterable

ARCHITECTURE_STATUS = "experimental"


class CampaignKind(StrEnum):
    """Business campaign families that share the same lifecycle contract."""

    JOIN = "join"
    COMMENT = "comment"


class CampaignStatus(StrEnum):
    """Canonical persisted campaign states.

    Values intentionally remain lowercase strings so existing SQLite rows and
    API payloads can adopt this enum later without a schema rewrite.
    """

    DRAFT = "draft"
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED_USER = "paused_user"
    PAUSED_FLOODWAIT = "paused_floodwait"
    PAUSED_RESTRICTION = "paused_restriction"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    """Result state for one concrete Telegram delivery attempt."""

    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class TaskStatus(StrEnum):
    """Technical queue-task lifecycle.

    A task is an execution mechanism, not the source of truth for campaign or
    delivery state.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidCampaignTransition(ValueError):
    """Raised when a forbidden campaign transition is requested."""


_TERMINAL_CAMPAIGN_STATUSES: Final[frozenset[CampaignStatus]] = frozenset(
    {
        CampaignStatus.COMPLETED,
        CampaignStatus.CANCELLED,
        CampaignStatus.FAILED,
    }
)

_ALLOWED_CAMPAIGN_TRANSITIONS: Final = MappingProxyType(
    {
        CampaignStatus.DRAFT: frozenset(
            {CampaignStatus.PLANNED, CampaignStatus.CANCELLED}
        ),
        CampaignStatus.PLANNED: frozenset(
            {
                CampaignStatus.RUNNING,
                CampaignStatus.CANCELLED,
                CampaignStatus.FAILED,
            }
        ),
        CampaignStatus.RUNNING: frozenset(
            {
                CampaignStatus.PAUSED_USER,
                CampaignStatus.PAUSED_FLOODWAIT,
                CampaignStatus.PAUSED_RESTRICTION,
                CampaignStatus.COMPLETED,
                CampaignStatus.CANCELLED,
                CampaignStatus.FAILED,
            }
        ),
        CampaignStatus.PAUSED_USER: frozenset(
            {
                CampaignStatus.RUNNING,
                CampaignStatus.CANCELLED,
                CampaignStatus.FAILED,
            }
        ),
        CampaignStatus.PAUSED_FLOODWAIT: frozenset(
            {
                CampaignStatus.RUNNING,
                CampaignStatus.PAUSED_RESTRICTION,
                CampaignStatus.CANCELLED,
                CampaignStatus.FAILED,
            }
        ),
        CampaignStatus.PAUSED_RESTRICTION: frozenset(
            {
                CampaignStatus.RUNNING,
                CampaignStatus.CANCELLED,
                CampaignStatus.FAILED,
            }
        ),
        CampaignStatus.COMPLETED: frozenset(),
        CampaignStatus.CANCELLED: frozenset(),
        CampaignStatus.FAILED: frozenset(),
    }
)


def coerce_campaign_status(value: CampaignStatus | str) -> CampaignStatus:
    """Convert a persisted or API value to the canonical enum."""

    if isinstance(value, CampaignStatus):
        return value
    try:
        return CampaignStatus(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"Unknown campaign status: {value!r}") from exc


def allowed_campaign_transitions(
    current: CampaignStatus | str,
) -> frozenset[CampaignStatus]:
    """Return immutable allowed targets for ``current``."""

    return _ALLOWED_CAMPAIGN_TRANSITIONS[coerce_campaign_status(current)]


def is_terminal_campaign_status(value: CampaignStatus | str) -> bool:
    """Return whether a campaign cannot leave its current state."""

    return coerce_campaign_status(value) in _TERMINAL_CAMPAIGN_STATUSES


def can_transition_campaign(
    current: CampaignStatus | str,
    target: CampaignStatus | str,
    *,
    allow_idempotent: bool = True,
) -> bool:
    """Return whether the requested transition is valid."""

    current_status = coerce_campaign_status(current)
    target_status = coerce_campaign_status(target)
    if allow_idempotent and current_status == target_status:
        return True
    return target_status in _ALLOWED_CAMPAIGN_TRANSITIONS[current_status]


def require_campaign_transition(
    current: CampaignStatus | str,
    target: CampaignStatus | str,
    *,
    allow_idempotent: bool = True,
) -> tuple[CampaignStatus, CampaignStatus]:
    """Validate and return normalized transition endpoints.

    Persistence services should call this before a compare-and-set SQL update.
    Validation alone does not provide database atomicity.
    """

    current_status = coerce_campaign_status(current)
    target_status = coerce_campaign_status(target)
    if not can_transition_campaign(
        current_status,
        target_status,
        allow_idempotent=allow_idempotent,
    ):
        allowed = ", ".join(
            sorted(status.value for status in allowed_campaign_transitions(current_status))
        )
        raise InvalidCampaignTransition(
            f"Campaign transition {current_status.value!r} -> "
            f"{target_status.value!r} is not allowed; "
            f"allowed targets: {allowed or '<none>'}"
        )
    return current_status, target_status


def validate_campaign_path(
    statuses: Iterable[CampaignStatus | str],
    *,
    allow_idempotent: bool = True,
) -> tuple[CampaignStatus, ...]:
    """Validate a complete lifecycle path, useful for recovery and tests."""

    normalized = tuple(coerce_campaign_status(status) for status in statuses)
    for current, target in zip(normalized, normalized[1:]):
        require_campaign_transition(
            current,
            target,
            allow_idempotent=allow_idempotent,
        )
    return normalized
