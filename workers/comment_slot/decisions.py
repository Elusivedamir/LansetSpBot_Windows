"""Pure outcome tables for one durable comment-slot execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeferredCommentDisposition(StrEnum):
    QUIET_HOURS = "quiet_hours"
    LOCAL_BAN = "local_ban"
    SHUTDOWN = "shutdown"
    NETWORK_WAIT = "network_wait"


@dataclass(frozen=True, slots=True)
class NonRetryableCommentDecision:
    friendly: str
    final_status: str
    consume_channel: bool
    pause_campaign: bool
    negative_cache_ttl: int | None = None


_NEGATIVE_TTLS = {
    "chat_write_forbidden": 7 * 24 * 60 * 60,
    "plain_text_forbidden": 7 * 24 * 60 * 60,
    "chat_restricted": 7 * 24 * 60 * 60,
    "permission_denied": 30 * 24 * 60 * 60,
    "channel_private": 30 * 24 * 60 * 60,
    "linked_chat_inaccessible": 7 * 24 * 60 * 60,
    "comments_disabled": 24 * 60 * 60,
    "message_id_invalid": 24 * 60 * 60,
}

_FRIENDLY = {
    "join_required": "Пропущено: сначала подготовьте участие во вкладке «Связки»",
    "chat_write_forbidden": "Пропущено: аккаунту запрещено писать в обсуждении",
    "user_banned": "Кампания остановлена: Telegram ограничил отправку аккаунта",
    "message_too_long": "Пропущено: текст превышает лимит Telegram",
    "entity_bounds_invalid": "Пропущено: некорректная разметка текста",
    "user_blocked": "Пропущено: получатель заблокирован",
    "plain_text_forbidden": "Пропущено: в обсуждении запрещены обычные текстовые сообщения",
    "chat_restricted": "Пропущено: обсуждение ограничено Telegram",
    "privacy_restricted": "Пропущено: действие запрещено настройками приватности",
    "permission_denied": "Пропущено: приватное обсуждение требует инвайт или доступ",
    "channel_private": "Пропущено: приватное обсуждение недоступно аккаунту",
    "linked_chat_inaccessible": "Пропущено: группа обсуждения недоступна аккаунту",
    "comments_disabled": "Пропущено: у последнего поста комментарии отключены",
    "message_id_invalid": "Пропущено: ветка последнего поста удалена",
    "peer_flood": "Кампания остановлена: Telegram ограничил активность аккаунта",
    "user_restricted": "Кампания остановлена: Telegram ограничил аккаунт",
    "auth_key_duplicated": "Кампания остановлена: Telegram аннулировал дублирующий ключ сессии",
    "flood_wait_long": "Кампания приостановлена: Telegram запросил слишком долгое ожидание",
    "flood_wait_repeated": "Кампания приостановлена: Telegram повторно ограничил запросы",
    "slow_mode_wait_long": "Кампания приостановлена: слишком долгий медленный режим",
    "slow_mode_wait_repeated": "Кампания приостановлена: повторный slow mode",
    "delivery_result_unknown": "Кампания приостановлена: результат отправки неизвестен",
    "direct_message_result_unknown": "Кампания приостановлена: результат отправки в группу неизвестен",
    "join_result_unknown": "Кампания приостановлена: результат вступления неизвестен",
    "delivery_persist_failed": "Кампания приостановлена: комментарий отправлен, но подтверждение не сохранено",
    "direct_message_persist_failed": "Кампания приостановлена: сообщение в группу отправлено, но подтверждение не сохранено",
    "direct_message_duplicate_guard": "Кампания приостановлена: отправка в группу уже выполнялась или требует ручной проверки",
    "comment_already_reserved": "Пропущено: этот пост уже отправлялся или требует ручной проверки",
    "network_unavailable": "Нет соединения с Telegram. Кампания временно ожидает сеть",
    "account_state_mismatch": "Кампания приостановлена: Telegram-сессия не совпадает с локальным аккаунтом",
    "openai_output_invalid": (
        "Кампания приостановлена: OpenAI-комментарий нарушил локальные правила"
    ),
}

_UNCERTAIN_CODES = frozenset(
    {
        "delivery_result_unknown",
        "direct_message_result_unknown",
        "direct_message_duplicate_guard",
        "join_result_unknown",
    }
)

_PAUSE_CODES = frozenset(
    {
        "user_banned",
        "peer_flood",
        "user_restricted",
        "auth_key_duplicated",
        "flood_wait_long",
        "flood_wait_repeated",
        "slow_mode_wait_long",
        "slow_mode_wait_repeated",
        "delivery_result_unknown",
        "direct_message_result_unknown",
        "join_result_unknown",
        "delivery_persist_failed",
        "direct_message_persist_failed",
        "direct_message_duplicate_guard",
        "security_time_sync",
        "account_state_mismatch",
        "openai_output_invalid",
    }
)

_CONSUME_ACCESS_FAILURES = frozenset(
    {"linked_chat_inaccessible", "permission_denied", "channel_private"}
)


def deferred_comment_disposition(code: str) -> DeferredCommentDisposition:
    normalized = str(code or "")
    if normalized == "local_quiet_hours":
        return DeferredCommentDisposition.QUIET_HOURS
    if normalized == "local_ban_before_dispatch":
        return DeferredCommentDisposition.LOCAL_BAN
    if normalized == "shutdown_before_dispatch":
        return DeferredCommentDisposition.SHUTDOWN
    return DeferredCommentDisposition.NETWORK_WAIT


def nonretryable_comment_decision(code: str, fallback: str) -> NonRetryableCommentDecision:
    normalized = str(code or "")
    pause_campaign = normalized in _PAUSE_CODES
    final_status = (
        "uncertain"
        if normalized in _UNCERTAIN_CODES
        else "failed" if pause_campaign else "skipped"
    )
    # Preserve the legacy cooldown contract: most final Telegram outcomes
    # consume the selected channel. Only explicit relink/account-context
    # deferrals retain the rotation target. Account restriction codes are
    # overridden to non-consuming by the runner when restriction state is built.
    consume_channel = True
    if normalized in {
        "discussion_relink_deferred",
        "account_state_mismatch",
        "openai_output_invalid",
    }:
        consume_channel = False
    return NonRetryableCommentDecision(
        friendly=_FRIENDLY.get(normalized, fallback),
        final_status=final_status,
        consume_channel=consume_channel,
        pause_campaign=pause_campaign,
        negative_cache_ttl=_NEGATIVE_TTLS.get(normalized),
    )


def network_backoff_seconds(failure_count: int) -> int:
    steps = (60, 180, 300, 600, 1200, 1800)
    normalized = max(1, int(failure_count))
    return steps[min(normalized - 1, len(steps) - 1)]


def generated_draft_terminal_status(
    *, current_status: str | None, sent: bool, send_started: bool, slot_deferred: bool
) -> str | None:
    if sent:
        return "sent"
    if send_started:
        return "uncertain"
    if current_status in {"generated", "sending"}:
        return "cancelled" if slot_deferred else "failed"
    return current_status
