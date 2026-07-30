from __future__ import annotations

import asyncio
import logging

from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
)
from services.comment_engine import CommentEngine
from services.linked_chat_service import LinkedChatService
from services.telegram_service import TelegramService

log = logging.getLogger(__name__)


class CommentService:
    """Send comments/direct messages and persist successful results."""

    # These errors are definitive Telegram/preflight rejections: the send was not
    # accepted, so releasing the reservation is safe. Everything else remains
    # ``uncertain`` because retrying could duplicate a comment already accepted by
    # Telegram while the client was losing the response.
    SAFE_RESERVATION_RELEASE_CODES = frozenset(
        {
            "authorization_required",
            "channel_private",
            "comments_disabled",
            "flood_wait_long",
            "flood_wait_repeated",
            "invalid_payload",
            "linked_chat_inaccessible",
            "linked_chat_missing",
            "message_id_invalid",
            "network_unavailable",
            "account_state_mismatch",
            "peer_flood",
            "user_restricted",
            "auth_key_duplicated",
            "permission_denied",
            "security_time_sync",
            "join_required",
            "chat_write_forbidden",
            "user_banned",
            "message_too_long",
            "entity_bounds_invalid",
            "user_blocked",
            "plain_text_forbidden",
            "chat_restricted",
            "privacy_restricted",
            "slow_mode_wait_long",
            "slow_mode_wait_repeated",
            "telegram_not_configured",
        }
    )

    def __init__(
        self,
        telegram: TelegramService,
        linked_chat_service: LinkedChatService | None = None,
        db=None,
        activity_schedule=None,
    ) -> None:
        self.telegram = telegram
        self.linked_chat_service = linked_chat_service
        self.db = db
        self.activity_schedule = activity_schedule
        self.comment_engine = CommentEngine()

    def _select_text(self, fallback: str) -> str:
        # An explicitly selected variant must remain deterministic so the queue can
        # record exactly what was sent. Templates are a compatibility fallback only.
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        if self.db:
            profile_getter = getattr(self.db, "get_account_comment_profile", None)
            if callable(profile_getter):
                profile = profile_getter(touch=True)
                templates = [
                    str(value or "").strip()
                    for value in list(profile.get("comments") or [])[:10]
                    if str(value or "").strip()
                ]
            else:  # pragma: no cover - compatibility for minimal test doubles
                rows = self.db.get_templates()
                templates = [
                    str(row.get(f"text_{index}") or "").strip()
                    for row in rows
                    for index in range(1, 11)
                    if str(row.get(f"text_{index}") or "").strip()
                ]
            selected = self.comment_engine.random_comment(templates)
            if selected:
                return str(selected).strip()
        raise NonRetryableTelegramError(
            "No non-empty comment text or saved template", code="comment_text_missing"
        )

    async def ensure_and_send_comment(
        self,
        *,
        channel_id,
        post_message_id,
        text,
        linked_chat_id=None,
        reply_to=None,
        membership_ready: bool = False,
        account_id=None,
        campaign_id=None,
        action_type: str = "comment",
        dispatch_barrier=None,
    ):
        if channel_id is None:
            raise NonRetryableTelegramError(
                "channel_id is required for a channel-post comment",
                code="channel_id_missing",
            )

        def ensure_targets_are_not_locally_banned(*peer_ids) -> None:
            if not self.db:
                return
            ban_checker = getattr(self.db, "is_channel_locally_banned", None)
            if not callable(ban_checker):
                return
            for peer_id in peer_ids:
                if peer_id is None:
                    continue
                if ban_checker(peer_id, account_id=account_id) is True:
                    raise NonRetryableTelegramError(
                        "Channel or discussion peer is permanently excluded "
                        "after an ambiguous Join result",
                        code="channel_locally_banned",
                    )

        ensure_targets_are_not_locally_banned(channel_id)
        text = self._select_text(text)

        # Apply user-configured quiet hours before a delivery reservation and
        # before any mutating Telegram RPC. Manual comments remain explicit user
        # actions and are therefore not delayed by the automation schedule.
        if self.activity_schedule is not None and action_type != "manual_comment":
            self.activity_schedule.require_active()

        if len(text) > 4096:
            raise NonRetryableTelegramError(
                "Comment text exceeds Telegram's 4096-character limit",
                code="message_too_long",
            )

        if linked_chat_id is None:
            if self.linked_chat_service is None:
                raise NonRetryableTelegramError(
                    "LinkedChatService is unavailable",
                    code="linked_chat_service_missing",
                )
            linked_chat_id = await self.linked_chat_service.get_linked_chat_id(
                channel_id
            )

        if linked_chat_id is None:
            raise NonRetryableTelegramError(
                "Channel has no linked discussion chat", code="linked_chat_missing"
            )

        # The linked peer can be banned independently of the source channel.
        # Re-read both durable targets after route resolution and before a
        # delivery reservation is created.
        ensure_targets_are_not_locally_banned(channel_id, linked_chat_id)

        reserved = False
        if self.db:
            reserved = self.db.reserve_comment_delivery(
                channel_id,
                post_message_id,
                linked_chat_id=linked_chat_id,
                text=text,
                account_id=account_id,
                campaign_id=campaign_id,
                action_type=action_type,
            )
            if not reserved:
                raise NonRetryableTelegramError(
                    "This post already has a delivery reservation or receipt",
                    code="comment_already_reserved",
                )
        effective_reply_to = reply_to
        try:
            send_kwargs = {}
            if dispatch_barrier is not None:
                send_kwargs["dispatch_barrier"] = dispatch_barrier
            if reply_to is not None:
                try:
                    result = await self.telegram.send_comment(
                        channel_id,
                        post_message_id,
                        text,
                        reply_to=reply_to,
                        linked_chat_id=linked_chat_id,
                        **send_kwargs,
                    )
                except NonRetryableTelegramError as exc:
                    if getattr(exc, "code", "") != "message_id_invalid":
                        raise
                    log.warning(
                        "Resolved discussion root became invalid; falling back to "
                        "Telegram comment_to routing for channel_id=%s post_id=%s",
                        channel_id,
                        post_message_id,
                    )
                    effective_reply_to = None
                    result = await self.telegram.send_comment(
                        channel_id,
                        post_message_id,
                        text,
                        reply_to=None,
                        **send_kwargs,
                    )
            else:
                result = await self.telegram.send_comment(
                    channel_id,
                    post_message_id,
                    text,
                    reply_to=None,
                    **send_kwargs,
                )
        except asyncio.CancelledError:
            if self.db and reserved:
                self.db.mark_comment_delivery_uncertain(
                    channel_id,
                    post_message_id,
                    "Operation cancelled after delivery reservation",
                    account_id=account_id,
                    linked_chat_id=linked_chat_id,
                    campaign_id=campaign_id,
                    action_type=action_type,
                )
            raise
        except DeferredTelegramError as exc:
            # FloodWait/SlowMode deferral is returned before Telegram executes the
            # mutating request, therefore a later queued attempt may reserve again.
            if self.db and reserved:
                self.db.release_comment_delivery(
                    channel_id,
                    post_message_id,
                    error=str(exc),
                    account_id=account_id,
                    linked_chat_id=linked_chat_id,
                    campaign_id=campaign_id,
                    action_type=action_type,
                )
            raise
        except NonRetryableTelegramError as exc:
            if self.db and reserved:
                if getattr(exc, "code", "") in self.SAFE_RESERVATION_RELEASE_CODES:
                    self.db.release_comment_delivery(
                        channel_id,
                        post_message_id,
                        error=str(exc),
                        account_id=account_id,
                        linked_chat_id=linked_chat_id,
                        campaign_id=campaign_id,
                        action_type=action_type,
                    )
                else:
                    self.db.mark_comment_delivery_uncertain(
                        channel_id,
                        post_message_id,
                        str(exc),
                        account_id=account_id,
                        linked_chat_id=linked_chat_id,
                        campaign_id=campaign_id,
                        action_type=action_type,
                    )
            raise
        except Exception as exc:
            if self.db and reserved:
                self.db.mark_comment_delivery_uncertain(
                    channel_id,
                    post_message_id,
                    f"{type(exc).__name__}: {exc}",
                    account_id=account_id,
                    linked_chat_id=linked_chat_id,
                    campaign_id=campaign_id,
                    action_type=action_type,
                )
            raise

        try:
            result_message_id = int(getattr(result, "id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            result_message_id = 0
        if result_message_id <= 0:
            message = (
                "Telegram did not return a confirmed comment message id; "
                "automatic replay is blocked"
            )
            if self.db and reserved:
                self.db.mark_comment_delivery_uncertain(
                    channel_id,
                    post_message_id,
                    message,
                    account_id=account_id,
                    linked_chat_id=linked_chat_id,
                    campaign_id=campaign_id,
                    action_type=action_type,
                )
            raise NonRetryableTelegramError(message, code="delivery_result_unknown")

        if self.db:
            data = {
                "channel_id": channel_id,
                "linked_chat_id": linked_chat_id,
                "post_message_id": post_message_id,
                "comment_message_id": result_message_id,
                "reply_to": effective_reply_to or post_message_id,
                "author_id": getattr(result, "sender_id", None),
                "text": text,
                "date": str(getattr(result, "date", "")) or None,
                "account_id": account_id,
                "campaign_id": campaign_id,
                "action_type": action_type,
            }
            try:
                self.db.finalize_comment_delivery(data)
            except Exception as exc:
                log.exception(
                    "Comment was sent but its durable receipt could not be finalized"
                )
                try:
                    self.db.mark_comment_delivery_uncertain(
                        channel_id,
                        post_message_id,
                        str(exc),
                        account_id=account_id,
                        linked_chat_id=linked_chat_id,
                        campaign_id=campaign_id,
                        action_type=action_type,
                    )
                except Exception:
                    log.exception("Could not mark the delivery as uncertain")
                raise NonRetryableTelegramError(
                    "Комментарий отправлен, но подтверждение не сохранено. Кампания остановлена для защиты от дубля.",
                    code="delivery_persist_failed",
                ) from exc
        log.info(
            "Comment sent successfully: account_id=%s campaign_id=%s "
            "channel_id=%s post_id=%s linked_chat_id=%s comment_message_id=%s",
            account_id,
            campaign_id,
            channel_id,
            post_message_id,
            linked_chat_id,
            result_message_id,
        )
        return result

    async def send_direct_message(
        self,
        chat_id,
        text,
        *,
        task_id=None,
        account_id=None,
        campaign_id=None,
        dispatch_barrier=None,
    ):
        """Reject the removed standalone ordinary-group delivery path."""

        raise NonRetryableTelegramError(
            "Direct messages to ordinary groups are disabled",
            code="direct_group_disabled",
        )

        selected = self._select_text(text)
        if len(selected) > 4096:
            raise NonRetryableTelegramError(
                "Message text exceeds Telegram's 4096-character limit",
                code="message_too_long",
            )
        if self.activity_schedule is not None:
            self.activity_schedule.require_active()

        try:
            durable_task_id = int(task_id or 0)
        except (TypeError, ValueError, OverflowError):
            durable_task_id = 0
        reserved = False
        if self.db:
            if durable_task_id <= 0:
                raise NonRetryableTelegramError(
                    "Direct group delivery requires a positive task id",
                    code="invalid_payload",
                )
            ban_checker = getattr(self.db, "is_channel_locally_banned", None)
            if callable(ban_checker) and ban_checker(chat_id, account_id=account_id):
                raise NonRetryableTelegramError(
                    "Direct group is permanently excluded",
                    code="channel_locally_banned",
                )
            reserved = self.db.reserve_direct_message_delivery(
                durable_task_id, chat_id, selected
            )
            if not reserved:
                raise NonRetryableTelegramError(
                    "Direct group delivery is already reserved or uncertain",
                    code="direct_message_duplicate_guard",
                )

        try:
            resolver = getattr(self.telegram, "_resolve_peer_reference", None)
            target = resolver(chat_id) if callable(resolver) else chat_id
            result = await self.telegram.send_message(
                target,
                selected,
                unknown_result_code="direct_message_result_unknown",
                dispatch_barrier=dispatch_barrier,
            )
        except asyncio.CancelledError:
            if self.db and reserved:
                self.db.mark_direct_message_delivery_uncertain(
                    durable_task_id,
                    "Operation cancelled after delivery reservation",
                )
            raise
        except DeferredTelegramError:
            if self.db and reserved:
                self.db.release_direct_message_delivery(durable_task_id)
            raise
        except NonRetryableTelegramError as exc:
            if self.db and reserved:
                if getattr(exc, "code", "") in self.SAFE_RESERVATION_RELEASE_CODES:
                    self.db.release_direct_message_delivery(durable_task_id)
                else:
                    self.db.mark_direct_message_delivery_uncertain(
                        durable_task_id, str(exc)
                    )
            raise
        except Exception as exc:
            if self.db and reserved:
                self.db.mark_direct_message_delivery_uncertain(
                    durable_task_id, f"{type(exc).__name__}: {exc}"
                )
            raise

        try:
            message_id = int(getattr(result, "id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            message_id = 0
        if message_id <= 0:
            message = (
                "Telegram did not return a confirmed direct-message id; "
                "automatic replay is blocked"
            )
            if self.db and reserved:
                self.db.mark_direct_message_delivery_uncertain(
                    durable_task_id, message
                )
            raise NonRetryableTelegramError(
                message, code="direct_message_result_unknown"
            )

        if self.db and reserved:
            try:
                self.db.finalize_direct_message_delivery(
                    durable_task_id, message_id=message_id
                )
            except Exception as exc:
                log.exception(
                    "Direct message was sent but its receipt could not be finalized"
                )
                try:
                    self.db.mark_direct_message_delivery_uncertain(
                        durable_task_id, str(exc)
                    )
                except Exception:
                    log.exception(
                        "Could not mark direct-message delivery as uncertain"
                    )
                raise NonRetryableTelegramError(
                    "Сообщение отправлено, но подтверждение не сохранено. "
                    "Кампания остановлена для защиты от дубля.",
                    code="direct_message_persist_failed",
                ) from exc

        log.info(
            "Direct group message sent successfully: task_id=%s account_id=%s "
            "campaign_id=%s chat_id=%s message_id=%s",
            durable_task_id,
            account_id,
            campaign_id,
            chat_id,
            message_id,
        )
        return result

    async def send_comment(
        self,
        *,
        channel_id,
        post_message_id,
        text,
        linked_chat_id=None,
        reply_to=None,
        account_id=None,
    ):
        return await self.ensure_and_send_comment(
            channel_id=channel_id,
            linked_chat_id=linked_chat_id,
            post_message_id=post_message_id,
            text=text,
            reply_to=reply_to,
            account_id=account_id,
        )

