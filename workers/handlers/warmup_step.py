from __future__ import annotations

import hashlib
import logging
from typing import Any

from telethon import functions, types

from core.campaign_schedule import from_db_time, utc_now
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from core.redaction import sanitize_exception

log = logging.getLogger(__name__)

_REACTION_EMOJIS = ("👍", "❤️", "🔥", "👏")
_REACTION_SKIP_ERRORS = {
    "reaction_invalid",
    "chat_write_forbidden",
    "message_id_invalid",
    "message_not_modified",
    "ReactionInvalidError",
    "ChatWriteForbiddenError",
    "MessageIdInvalidError",
    "MessageNotModifiedError",
}
_PENDING_MEMBERSHIP_ERRORS = {"join_requested", "InviteRequestSentError"}
_UNAVAILABLE_GROUP_ERRORS = {
    "channel_private",
    "permission_denied",
    "invite_expired",
    "invite_invalid",
    "username_invalid",
    "username_not_found",
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "InviteHashExpiredError",
    "InviteHashInvalidError",
    "UsernameInvalidError",
    "UsernameNotOccupiedError",
}
_BLOCKED_GROUP_ERRORS = {"user_banned", "UserBannedInChannelError"}
_ALREADY_MEMBER_ERRORS = {"UserAlreadyParticipantError"}


def _warmup_error_key(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or "").strip()
    return code or type(exc).__name__


def _unknown_result(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "").lower()
    return any(
        marker in code
        for marker in (
            "unknown",
            "unconfirmed",
            "uncertain",
            "confirmation_lost",
            "result_lost",
        )
    )


def _stable_message_random_id(*, pair_id: int, step_id: int, account_id: int) -> int:
    """Return the same signed MTProto random_id for every retry of one step."""
    payload = f"lanset-warmup:{pair_id}:{step_id}:{account_id}".encode("utf-8")
    value = int.from_bytes(
        hashlib.blake2b(payload, digest_size=8).digest(),
        byteorder="little",
        signed=True,
    )
    return value or 1


def _extract_sent_message_id(result: object) -> int:
    """Read a sent message id from Updates and short-update response variants."""
    visited: set[int] = set()

    def visit(value: object) -> int:
        if value is None:
            return 0
        marker = id(value)
        if marker in visited:
            return 0
        visited.add(marker)
        if type(value).__name__ in {
            "Message",
            "MessageService",
            "UpdateShortSentMessage",
            "UpdateShortMessage",
            "UpdateShortChatMessage",
        }:
            candidate = int(getattr(value, "id", 0) or 0)
            if candidate > 0:
                return candidate
        for attribute in ("message", "update"):
            candidate = visit(getattr(value, attribute, None))
            if candidate > 0:
                return candidate
        for item in list(getattr(value, "updates", None) or []):
            candidate = visit(item)
            if candidate > 0:
                return candidate
        return 0

    return visit(result)


async def _recover_existing_message_id(
    *, telegram, peer: object, text: str, reply_to: int | None, dispatch_barrier
) -> int:
    """Resolve a previously accepted send after confirmation loss or duplicate id."""
    recent = await telegram.execute(
        telegram.client.get_messages,
        peer,
        limit=40,
        retry_network=True,
        dispatch_barrier=dispatch_barrier,
    )
    for item in list(recent or []):
        if not bool(getattr(item, "out", False)):
            continue
        body = str(getattr(item, "message", "") or "").strip()
        if body != text:
            continue
        direct_reply = int(getattr(item, "reply_to_msg_id", 0) or 0)
        nested_reply = int(
            getattr(getattr(item, "reply_to", None), "reply_to_msg_id", 0) or 0
        )
        actual_reply = direct_reply or nested_reply
        if reply_to is not None and actual_reply != int(reply_to):
            continue
        candidate = int(getattr(item, "id", 0) or 0)
        if candidate > 0:
            return candidate
    return 0


def _catchup_delay_seconds(
    *,
    step: dict[str, Any],
    pair: dict[str, Any],
    pair_id: int,
    step_id: int,
) -> int:
    """Return a deterministic human-scale delay for an overdue message step."""
    scheduled_at = from_db_time(step.get("scheduled_at"))
    if scheduled_at is None:
        return 0
    overdue_seconds = (utc_now() - scheduled_at).total_seconds()
    minimum = max(120, int(pair.get("reply_min_seconds") or 120))
    maximum = max(minimum, int(pair.get("reply_max_seconds") or 900))
    if overdue_seconds <= maximum:
        return 0
    digest = hashlib.blake2b(
        f"lanset-warmup-catchup:{pair_id}:{step_id}".encode("utf-8"),
        digest_size=8,
    ).digest()
    span = maximum - minimum + 1
    return minimum + (int.from_bytes(digest, "big") % span)


async def _resolve_verified_user_peer(
    *,
    telegram,
    target: dict[str, Any],
    target_account_id: int,
    dispatch_barrier,
) -> object:
    """Resolve a Telegram user and verify immutable user-id identity before activity."""
    username = str(target.get("username") or "").strip().lstrip("@")
    locators: list[object] = [target_account_id]
    if username:
        locators.append(username)

    last_error: BaseException | None = None
    for locator in locators:
        try:
            input_peer = await telegram.execute(
                telegram.client.get_input_entity,
                locator,
                retry_network=True,
                dispatch_barrier=dispatch_barrier,
            )
        except (DeferredTelegramError, NonRetryableTelegramError):
            raise
        except Exception as exc:
            last_error = exc
            continue

        resolved_user_id = int(getattr(input_peer, "user_id", 0) or 0)
        if resolved_user_id != target_account_id:
            raise NonRetryableTelegramError(
                "Telegram peer не соответствует связанному аккаунту",
                code="warmup_partner_identity_mismatch",
            )
        return input_peer

    raise NonRetryableTelegramError(
        "Не удалось безопасно определить Telegram peer связанного аккаунта",
        code="warmup_partner_unresolvable",
    ) from last_error


async def _resolve_receiver_local_message_id(
    *,
    telegram,
    peer: object,
    account_id: int,
    target_account_id: int,
    previous_context: dict[str, Any] | None,
    dispatch_barrier,
) -> int:
    """Map the previous sent message to the receiver account's local Message.id."""
    if not previous_context:
        return 0

    previous_actor = int(previous_context.get("actor_account_id") or 0)
    previous_target = int(previous_context.get("target_account_id") or 0)
    previous_text = str(previous_context.get("message_text") or "").strip()
    if (
        previous_actor != target_account_id
        or previous_target != account_id
        or not previous_text
    ):
        return 0

    completed_at = from_db_time(previous_context.get("completed_at"))
    recent = await telegram.execute(
        telegram.client.get_messages,
        peer,
        limit=60,
        retry_network=True,
        dispatch_barrier=dispatch_barrier,
    )

    candidates: list[int] = []
    for item in list(recent or []):
        if bool(getattr(item, "out", False)):
            continue
        body = str(getattr(item, "message", "") or "").strip()
        if body != previous_text:
            continue

        sender_id = int(
            getattr(item, "sender_id", 0)
            or getattr(getattr(item, "from_id", None), "user_id", 0)
            or 0
        )
        if sender_id > 0 and sender_id != target_account_id:
            continue

        if completed_at is not None:
            message_time = from_db_time(getattr(item, "date", None))
            if (
                message_time is not None
                and abs((message_time - completed_at).total_seconds()) > 15 * 60
            ):
                continue

        candidate = int(getattr(item, "id", 0) or 0)
        if candidate > 0:
            candidates.append(candidate)

    if len(candidates) != 1:
        return 0
    return candidates[0]


def _group_join_parts(chat_ref: str) -> tuple[str | None, str | None]:
    value = str(chat_ref or "").strip()
    if any(marker in value for marker in ("t.me/+", "joinchat/")):
        return None, value
    if value.startswith(("https://t.me/", "http://t.me/")):
        username = value.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        return username or None, None
    return value.lstrip("@") or None, None


def create_warmup_step_handler(
    *,
    queue_worker,
    worker_db,
    telegram,
    set_runtime,
    publish_activity,
    contact_phone_provider,
):
    async def warmup_step(task: dict[str, Any]) -> None:
        payload = dict(task.get("payload") or {})
        task_id = int(task.get("id") or 0)
        account_id = int(payload.get("account_id") or 0)
        pair_id = int(payload.get("pair_id") or 0)
        step_id = int(payload.get("step_id") or 0)
        if account_id <= 0 or pair_id <= 0 or step_id <= 0:
            raise NonRetryableTelegramError(
                "Некорректный шаг прогрева", code="warmup_invalid_payload"
            )

        set_runtime(
            task_id,
            f"Прогрев · связка #{pair_id}",
            account_id=account_id,
        )
        step = worker_db.begin_warmup_step(step_id, account_id=account_id)
        if step is None:
            publish_activity(
                f"Связка #{pair_id}: шаг пропущен, потому что прогрев остановлен",
                category="Прогрев",
            )
            return
        if bool(step.get("already_finished")):
            worker_db.enqueue_warmup_step(pair_id)
            return

        owner_token = str(step.get("owner_token") or "")
        try:
            # Claim finalization must cover lease/barrier setup too. If either
            # setup operation fails after begin_warmup_step() marked the step
            # running, the common exception handlers below persist the failure
            # instead of leaving a durable step stuck in running state.
            worker_db.acquire_account_activity_lease(
                account_id,
                owner_token=owner_token,
                lease_seconds=30 * 60,
                metadata={"pair_id": pair_id, "step_id": step_id, "source": "queue"},
            )
            barrier = queue_worker.create_scope_dispatch_barrier(
                ("warmup_pair", pair_id),
                ("account", account_id),
                pre_dispatch_check=lambda: bool(
                    (worker_db.get_warmup_pair(pair_id) or {}).get("status") == "running"
                ),
            )

            action = str(step.get("action") or "")
            telegram_message_id: int | None = None
            result_text = ""
            skipped = False

            if action == "ensure_contact":
                target_account_id = int(step.get("target_account_id") or 0)
                target = worker_db.get_telegram_account(target_account_id)
                if not target:
                    raise NonRetryableTelegramError(
                        "Связанный аккаунт не найден",
                        code="warmup_partner_missing",
                    )
                phone = str(contact_phone_provider(target_account_id) or "").strip()
                if not phone:
                    raise NonRetryableTelegramError(
                        "У связанного аккаунта нет сохранённого телефона",
                        code="warmup_partner_phone_missing",
                    )
                display_name = str(target.get("display_name") or "Контакт").strip()
                parts = display_name.split(maxsplit=1)
                first_name = parts[0][:64] or "Контакт"
                last_name = parts[1][:64] if len(parts) > 1 else ""
                contact = types.InputPhoneContact(
                    client_id=(pair_id << 32) ^ target_account_id,
                    phone=phone,
                    first_name=first_name,
                    last_name=last_name,
                )
                request = functions.contacts.ImportContactsRequest(contacts=[contact])
                await telegram.execute(
                    telegram.client,
                    request,
                    retry_network=False,
                    unknown_result_code="warmup_contact_result_unknown",
                    dispatch_barrier=barrier,
                )
                result_text = "Связанный аккаунт добавлен в контакты"
                publish_activity(
                    f"Связка #{pair_id}: контакт связанного аккаунта подготовлен",
                    category="Прогрев",
                )

            elif action == "message":
                target_account_id = int(step.get("target_account_id") or 0)
                target = worker_db.get_telegram_account(target_account_id)
                if not target:
                    raise NonRetryableTelegramError(
                        "Связанный аккаунт не найден",
                        code="warmup_partner_missing",
                    )
                text = str(step.get("message_text") or "").strip()
                if not text:
                    raise NonRetryableTelegramError(
                        "Пустая реплика сценария",
                        code="warmup_empty_message",
                    )
                typing_seconds = max(1, min(12, int(step.get("typing_seconds") or 3)))
                input_peer = await _resolve_verified_user_peer(
                    telegram=telegram,
                    target=target,
                    target_account_id=target_account_id,
                    dispatch_barrier=barrier,
                )

                pair_state = dict(worker_db.get_warmup_pair(pair_id) or {})
                catchup_delay = _catchup_delay_seconds(
                    step=step,
                    pair=pair_state,
                    pair_id=pair_id,
                    step_id=step_id,
                )
                if catchup_delay > 0:
                    set_runtime(task_id, f"Восстановление темпа связки #{pair_id}")
                    completed_sleep = await queue_worker.safe_sleep(
                        catchup_delay,
                        cancel_scope=("warmup_pair", pair_id),
                    )
                    if not completed_sleep:
                        raise NonRetryableTelegramError(
                            "Прогрев остановлен до отправки сообщения",
                            code="warmup_cancelled_before_send",
                        )

                reply_to = None
                if bool(step.get("reply_to_previous")):
                    previous_context = worker_db.get_previous_warmup_message_context(
                        pair_id=pair_id,
                        week_number=int(step.get("week_number") or 0),
                        before_sequence_no=int(step.get("sequence_no") or 0),
                    )
                    reply_to = await _resolve_receiver_local_message_id(
                        telegram=telegram,
                        peer=input_peer,
                        account_id=account_id,
                        target_account_id=target_account_id,
                        previous_context=previous_context,
                        dispatch_barrier=barrier,
                    )

                set_runtime(task_id, f"Диалог связки #{pair_id}")
                async with telegram.client.action(input_peer, "typing"):
                    completed_sleep = await queue_worker.safe_sleep(
                        typing_seconds,
                        cancel_scope=("warmup_pair", pair_id),
                    )
                    if not completed_sleep:
                        raise NonRetryableTelegramError(
                            "Прогрев остановлен до отправки сообщения",
                            code="warmup_cancelled_before_send",
                        )
                reply_input = (
                    types.InputReplyToMessage(reply_to_msg_id=int(reply_to))
                    if reply_to is not None
                    else None
                )
                send_request = functions.messages.SendMessageRequest(
                    peer=input_peer,
                    message=text,
                    random_id=_stable_message_random_id(
                        pair_id=pair_id,
                        step_id=step_id,
                        account_id=account_id,
                    ),
                    no_webpage=True,
                    reply_to=reply_input,
                )
                try:
                    send_result = await telegram.execute(
                        telegram.client,
                        send_request,
                        retry_network=False,
                        unknown_result_code="warmup_message_result_unknown",
                        dispatch_barrier=barrier,
                    )
                    telegram_message_id = _extract_sent_message_id(send_result)
                except NonRetryableTelegramError as exc:
                    if str(getattr(exc, "code", "") or "") != (
                        "message_random_id_duplicate"
                    ):
                        raise
                    telegram_message_id = await _recover_existing_message_id(
                        telegram=telegram,
                        peer=input_peer,
                        text=text,
                        reply_to=reply_to,
                        dispatch_barrier=barrier,
                    )
                if telegram_message_id <= 0:
                    telegram_message_id = await _recover_existing_message_id(
                        telegram=telegram,
                        peer=input_peer,
                        text=text,
                        reply_to=reply_to,
                        dispatch_barrier=barrier,
                    )
                if telegram_message_id <= 0:
                    raise NonRetryableTelegramError(
                        "Telegram не подтвердил отправку реплики",
                        code="warmup_message_result_unknown",
                    )
                result_text = "Реплика отправлена"
                publish_activity(
                    f"Связка #{pair_id}: отправлена реплика сценария",
                    category="Прогрев",
                )

            elif action == "private_reaction":
                target_account_id = int(step.get("target_account_id") or 0)
                target = worker_db.get_telegram_account(target_account_id)
                if not target:
                    raise NonRetryableTelegramError(
                        "Связанный аккаунт не найден",
                        code="warmup_partner_missing",
                    )
                input_peer = await _resolve_verified_user_peer(
                    telegram=telegram,
                    target=target,
                    target_account_id=target_account_id,
                    dispatch_barrier=barrier,
                )
                previous_context = worker_db.get_previous_warmup_message_context(
                    pair_id=pair_id,
                    week_number=int(step.get("week_number") or 0),
                    before_sequence_no=int(step.get("sequence_no") or 0),
                )
                previous_message_id = await _resolve_receiver_local_message_id(
                    telegram=telegram,
                    peer=input_peer,
                    account_id=account_id,
                    target_account_id=target_account_id,
                    previous_context=previous_context,
                    dispatch_barrier=barrier,
                )
                if previous_message_id <= 0:
                    skipped = True
                    result_text = (
                        "Не удалось однозначно определить локальный ID "
                        "предыдущего сообщения"
                    )
                else:
                    emoji = _REACTION_EMOJIS[step_id % len(_REACTION_EMOJIS)]
                    reaction_request = functions.messages.SendReactionRequest(
                        peer=input_peer,
                        msg_id=previous_message_id,
                        reaction=[types.ReactionEmoji(emoticon=emoji)],
                    )
                    try:
                        await telegram.execute(
                            telegram.client,
                            reaction_request,
                            retry_network=False,
                            unknown_result_code="warmup_private_reaction_result_unknown",
                            dispatch_barrier=barrier,
                        )
                        result_text = "Реакция в личном диалоге поставлена"
                        publish_activity(
                            f"Связка #{pair_id}: поставлена реакция в диалоге",
                            category="Прогрев",
                        )
                    except Exception as exc:
                        if _warmup_error_key(exc) in _REACTION_SKIP_ERRORS:
                            skipped = True
                            result_text = "Реакция недоступна в этом диалоге"
                        else:
                            raise

            elif action == "group_visit":
                group = worker_db.choose_warmup_group_for_account(account_id)
                if not group:
                    skipped = True
                    result_text = "Нет добавленных групп для прогрева"
                else:
                    group_id = int(group["id"])
                    chat_ref = str(group["chat_ref"])
                    username, invite_link = _group_join_parts(chat_ref)
                    membership_state = str(group.get("membership_state") or "unknown")
                    if membership_state not in {"joined", "requested"}:
                        try:
                            await telegram.join_saved_dialog(
                                username=username,
                                invite_link=invite_link,
                                dispatch_barrier=barrier,
                            )
                        except Exception as exc:
                            name = _warmup_error_key(exc)
                            if name not in _ALREADY_MEMBER_ERRORS:
                                if name in _PENDING_MEMBERSHIP_ERRORS:
                                    worker_db.record_warmup_group_visit(
                                        group_id=group_id,
                                        account_id=account_id,
                                        membership_state="requested",
                                        error=sanitize_exception(exc),
                                    )
                                    skipped = True
                                    result_text = "Отправлена заявка на вступление"
                                elif name in _BLOCKED_GROUP_ERRORS:
                                    worker_db.record_warmup_group_visit(
                                        group_id=group_id,
                                        account_id=account_id,
                                        membership_state="blocked",
                                        error=sanitize_exception(exc),
                                    )
                                    skipped = True
                                    result_text = "Аккаунт заблокирован в группе"
                                elif name in _UNAVAILABLE_GROUP_ERRORS:
                                    worker_db.record_warmup_group_visit(
                                        group_id=group_id,
                                        account_id=account_id,
                                        membership_state="unavailable",
                                        error=sanitize_exception(exc),
                                    )
                                    skipped = True
                                    result_text = "Группа недоступна этому аккаунту"
                                else:
                                    raise
                    if not skipped:
                        posts_to_read = max(
                            1, min(4, int(step.get("posts_to_read") or 2))
                        )
                        try:
                            messages = list(
                                await telegram.get_messages(
                                    chat_ref, limit=posts_to_read
                                )
                                or []
                            )
                        except Exception as exc:
                            name = _warmup_error_key(exc)
                            if name in _PENDING_MEMBERSHIP_ERRORS or (
                                membership_state == "requested"
                                and name in _UNAVAILABLE_GROUP_ERRORS
                            ):
                                worker_db.record_warmup_group_visit(
                                    group_id=group_id,
                                    account_id=account_id,
                                    membership_state="requested",
                                    error=sanitize_exception(exc),
                                )
                                skipped = True
                                result_text = "Доступ к постам ожидает одобрения"
                                messages = []
                            elif name in _BLOCKED_GROUP_ERRORS:
                                worker_db.record_warmup_group_visit(
                                    group_id=group_id,
                                    account_id=account_id,
                                    membership_state="blocked",
                                    error=sanitize_exception(exc),
                                )
                                skipped = True
                                result_text = "Аккаунт заблокирован в группе"
                                messages = []
                            elif name in _UNAVAILABLE_GROUP_ERRORS:
                                worker_db.record_warmup_group_visit(
                                    group_id=group_id,
                                    account_id=account_id,
                                    membership_state="unavailable",
                                    error=sanitize_exception(exc),
                                )
                                skipped = True
                                result_text = "Группа недоступна этому аккаунту"
                                messages = []
                            else:
                                raise
                        readable = [
                            item
                            for item in messages
                            if int(getattr(item, "id", 0) or 0) > 0
                        ]
                        last_read = max(
                            (int(getattr(item, "id", 0) or 0) for item in readable),
                            default=0,
                        )
                        reacted = 0
                        if last_read > 0:
                            peer = await telegram.execute(
                                telegram.client.get_input_entity,
                                chat_ref,
                                retry_network=True,
                                dispatch_barrier=barrier,
                            )
                            request = functions.messages.ReadHistoryRequest(
                                peer=peer, max_id=last_read
                            )
                            await telegram.execute(
                                telegram.client,
                                request,
                                retry_network=False,
                                unknown_result_code="warmup_read_history_result_unknown",
                                dispatch_barrier=barrier,
                            )
                            if bool(step.get("should_react")):
                                candidate = readable[0]
                                message_id = int(getattr(candidate, "id", 0) or 0)
                                emoji = _REACTION_EMOJIS[step_id % len(_REACTION_EMOJIS)]
                                reaction_request = functions.messages.SendReactionRequest(
                                    peer=peer,
                                    msg_id=message_id,
                                    reaction=[types.ReactionEmoji(emoticon=emoji)],
                                )
                                try:
                                    await telegram.execute(
                                        telegram.client,
                                        reaction_request,
                                        retry_network=False,
                                        unknown_result_code="warmup_group_reaction_result_unknown",
                                        dispatch_barrier=barrier,
                                    )
                                    reacted = message_id
                                except Exception as exc:
                                    if _warmup_error_key(exc) not in _REACTION_SKIP_ERRORS:
                                        raise
                            worker_db.record_warmup_group_visit(
                                group_id=group_id,
                                account_id=account_id,
                                membership_state="joined",
                                last_read_message_id=last_read,
                                last_reacted_message_id=reacted or None,
                            )
                            result_text = (
                                f"Прочитано постов: {len(readable)}"
                                + (" · реакция поставлена" if reacted else "")
                            )
                            publish_activity(
                                f"Связка #{pair_id}: посещена добавленная группа · {result_text}",
                                category="Прогрев",
                            )
                        elif not skipped:
                            worker_db.record_warmup_group_visit(
                                group_id=group_id,
                                account_id=account_id,
                                membership_state="joined",
                            )
                            skipped = True
                            result_text = "В группе нет доступных постов"
            else:
                raise NonRetryableTelegramError(
                    f"Неизвестное действие прогрева: {action}",
                    code="warmup_action_invalid",
                )

            outcome = worker_db.finish_warmup_step(
                step_id,
                telegram_message_id=telegram_message_id,
                result_text=result_text,
                skipped=skipped,
            )
            if bool(outcome.get("completed")):
                pair = dict(outcome.get("pair") or {})
                for related_account_id, token_key in (
                    (int(pair.get("account_a_id") or 0), "owner_token_a"),
                    (int(pair.get("account_b_id") or 0), "owner_token_b"),
                ):
                    token = str(pair.get(token_key) or "")
                    if related_account_id > 0 and token:
                        worker_db.release_account_activity_lease(
                            related_account_id, owner_token=token
                        )
                publish_activity(
                    f"Связка #{pair_id}: недельный прогрев завершён",
                    category="Прогрев",
                )
            else:
                worker_db.enqueue_warmup_step(pair_id)
                queue_worker.notify_task_available()
        except DeferredTelegramError:
            # QueueWorker persists FloodWait/network deferral on the same task.
            # The workflow step must become claimable again, but its queue task
            # id is retained so no second task is created for the same action.
            worker_db.defer_warmup_step(step_id, clear_queue_task=False)
            raise
        except NonRetryableTelegramError as exc:
            if str(getattr(exc, "code", "") or "") == "warmup_cancelled_before_send":
                worker_db.defer_warmup_step(step_id, clear_queue_task=True)
                return
            if _unknown_result(exc):
                retry_message = (
                    "Результат Telegram не подтверждён; автоматический повтор через 5 минут"
                )
                try:
                    worker_db.reschedule_warmup_step_after_unknown(
                        step_id,
                        delay_seconds=5 * 60,
                        message=retry_message,
                    )
                except Exception as reschedule_exc:
                    log.exception("Could not create durable five-minute warmup retry")
                    worker_db.defer_warmup_step(
                        step_id,
                        clear_queue_task=False,
                    )
                    raise DeferredTelegramError(
                        retry_message,
                        code="warmup_unknown_retry_deferred",
                        retry_after=5 * 60,
                    ) from reschedule_exc
                publish_activity(
                    f"Связка #{pair_id}: результат не подтверждён · повтор через 5 минут",
                    level="WARNING",
                    category="Прогрев",
                )
                queue_worker.notify_task_available()
                return
            try:
                worker_db.fail_warmup_step(
                    step_id,
                    message=str(exc),
                    uncertain=False,
                )
            except Exception:
                log.exception("Could not persist warmup step failure")
            publish_activity(
                f"Связка #{pair_id}: прогрев приостановлен · {sanitize_exception(exc)}",
                level="ERROR",
                category="Прогрев",
            )
            raise
        except Exception as exc:
            try:
                worker_db.fail_warmup_step(
                    step_id,
                    message=str(exc),
                    uncertain=False,
                )
            except Exception:
                log.exception("Could not persist warmup step failure")
            publish_activity(
                f"Связка #{pair_id}: прогрев приостановлен · {sanitize_exception(exc)}",
                level="ERROR",
                category="Прогрев",
            )
            raise

    return warmup_step
