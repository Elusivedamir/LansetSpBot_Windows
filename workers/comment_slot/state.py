"""Mutable state for one durable comment-slot workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.openai_settings import CommentGenerationSettings
from workers.comment_slot.models import CommentSlotPhase


@dataclass(slots=True)
class CommentSlotState:
    task_id: int
    payload: dict[str, Any]
    campaign_id: int
    slot_id: int
    campaign: dict[str, Any]
    campaign_account_id: int
    payload_account_id: int

    comment_source: str = "prepared"
    generation_settings: CommentGenerationSettings | None = None
    generation_prompt: str = ""
    variants: list[str] = field(default_factory=list)
    cancellation_scope: tuple[str, int] = field(init=False)

    cached_route: dict[str, Any] | None = None
    cached_channel_id: int = 0
    channel: dict[str, Any] | None = None
    channel_id: int = 0
    comment_mode: str = "channel_post"
    linked_chat_id: int | None = None
    channel_title: str = ""
    cached_post_id: int = 0
    post_id: int | None = None
    discussion_chat_id: int | None = None
    discussion_message_id: int | None = None
    resolved_result: Any | None = None

    selected: str | None = None
    generated_draft_id: int | None = None
    generated_draft_status: str | None = None
    generated_post_text: str = ""

    final_status: str = "skipped"
    final_message: str = "Пропущено"
    sent: bool = False
    slot_deferred: bool = False
    consume_channel: bool = True
    phase: CommentSlotPhase = CommentSlotPhase.PRECHECK
    internal_error: Exception | None = None
    campaign_pause_reason: str | None = None
    account_restriction: tuple[str, str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.cancellation_scope = ("comment_campaign", self.campaign_id)
