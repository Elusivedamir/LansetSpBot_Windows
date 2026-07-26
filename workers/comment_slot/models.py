from __future__ import annotations

from enum import IntEnum


class CommentSlotPhase(IntEnum):
    PRECHECK = 1
    MEMBERSHIP = 2
    READY_TO_SEND = 3
    SEND_STARTED = 4
    SEND_CONFIRMED = 5
