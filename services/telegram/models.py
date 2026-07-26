from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LatestPostResult:
    """Result of inspecting only the newest channel publication."""

    status: str
    message: Any | None = None
    discussion_chat_id: int | None = None
    discussion_message_id: int | None = None
