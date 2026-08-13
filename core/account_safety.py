from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AdaptiveSafetyLevel(StrEnum):
    NORMAL = "normal"
    CONSERVATIVE = "conservative"
    SOFT_PROTECTIVE = "soft_protective"


class EffectiveSafetyMode(StrEnum):
    NORMAL = "normal"
    CONSERVATIVE = "conservative"
    PROTECTIVE = "protective"


FLOOD_ESCALATION_WINDOW_SECONDS = 6 * 60 * 60
CONSERVATIVE_RECOVERY_SECONDS = 60 * 60
SOFT_PROTECTIVE_RECOVERY_SECONDS = 4 * 60 * 60
POST_PROTECTIVE_CONSERVATIVE_SECONDS = 60 * 60

SAFETY_PACED_TASK_TYPES = frozenset(
    {"auto_comment", "auto_comment_slot", "join_saved_slot", "link_channels", "warmup_step"}
)

_MUTATING_REQUEST_NAMES = frozenset(
    {
        "SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest",
        "SendReactionRequest", "JoinChannelRequest", "ImportChatInviteRequest",
        "ImportContactsRequest", "ReadHistoryRequest", "DeleteHistoryRequest",
        "DeleteMessagesRequest", "EditMessageRequest", "ForwardMessagesRequest",
    }
)

_REQUEST_SPACING_SECONDS = {
    "JoinChannelRequest": 120,
    "ImportChatInviteRequest": 120,
    "ImportContactsRequest": 90,
    "SendMessageRequest": 75,
    "SendMediaRequest": 75,
    "SendMultiMediaRequest": 75,
    "SendReactionRequest": 60,
    "ReadHistoryRequest": 45,
    "DeleteHistoryRequest": 75,
    "DeleteMessagesRequest": 75,
    "EditMessageRequest": 75,
    "ForwardMessagesRequest": 75,
}


@dataclass(frozen=True, slots=True)
class EffectiveSafety:
    mode: EffectiveSafetyMode
    adaptive_level: AdaptiveSafetyLevel
    hard_block: bool
    reason_code: str
    reason_text: str
    recovery_not_before: str
    pacing_multiplier: float


def telegram_request_name(request: Any) -> str:
    return type(request).__name__ if request is not None else "UnknownRequest"


def telegram_request_is_mutating(request: Any) -> bool:
    return telegram_request_name(request) in _MUTATING_REQUEST_NAMES


def conservative_request_spacing_seconds(request: Any) -> int:
    return int(_REQUEST_SPACING_SECONDS.get(telegram_request_name(request), 75))
