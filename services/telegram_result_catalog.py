from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class TelegramResultDisposition(StrEnum):
    FAILED = "failed"
    DEFERRED = "deferred"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class TelegramRpcResult:
    disposition: TelegramResultDisposition
    code: str
    message: str
    retry_after: int | None = None


# Exact Telethon class names.  Name-based matching avoids importing every generated
# error class and remains compatible when Telethon adds or removes individual names.
_EXACT_RESULTS: dict[str, tuple[str, str]] = {
    # Authorization/session.
    "AuthKeyDuplicatedError": ("auth_key_duplicated", "Telegram аннулировал дублированный ключ авторизации"),
    "AuthKeyInvalidError": ("auth_key_invalid", "Ключ авторизации Telegram недействителен"),
    "AuthKeyPermEmptyError": ("auth_key_invalid", "Постоянный ключ авторизации Telegram отсутствует"),
    "AuthKeyUnregisteredError": ("authorization_required", "Сессия Telegram больше не зарегистрирована"),
    "SessionExpiredError": ("authorization_required", "Сессия Telegram истекла"),
    "SessionRevokedError": ("authorization_required", "Сессия Telegram отозвана"),
    "SessionPasswordNeededError": ("two_factor_required", "Telegram требует пароль двухэтапной аутентификации"),
    "UserDeactivatedError": ("account_deactivated", "Telegram-аккаунт деактивирован"),
    "UserDeactivatedBanError": ("account_deactivated", "Telegram-аккаунт деактивирован или заблокирован"),
    "UserRestrictedError": ("user_restricted", "Telegram ограничил действия этого аккаунта"),
    # Peer/entity and usernames.
    "PeerIdInvalidError": ("peer_invalid", "Telegram не распознал получателя или чат"),
    "InputUserDeactivatedError": ("peer_deactivated", "Получатель Telegram деактивирован"),
    "UserIdInvalidError": ("user_invalid", "Идентификатор пользователя Telegram недействителен"),
    "ChannelInvalidError": ("channel_invalid", "Идентификатор канала или группы недействителен"),
    "ChatIdInvalidError": ("chat_invalid", "Идентификатор чата недействителен"),
    "UsernameInvalidError": ("username_invalid", "Имя пользователя или группы Telegram имеет неверный формат"),
    "UsernameNotOccupiedError": ("username_not_found", "Имя пользователя или группы Telegram не найдено"),
    "UsernameNotModifiedError": ("username_not_modified", "Имя пользователя уже имеет это значение"),
    # Membership/invites.
    "ChannelPrivateError": ("channel_private", "Группа или канал закрыты либо недоступны аккаунту"),
    "UserNotParticipantError": ("join_required", "Аккаунт не является участником группы"),
    "UserBannedInChannelError": ("user_banned", "Аккаунт заблокирован в этой группе или канале"),
    "InviteHashEmptyError": ("invite_empty", "Ссылка-приглашение Telegram пуста"),
    "InviteHashExpiredError": ("invite_expired", "Ссылка-приглашение Telegram истекла"),
    "InviteHashInvalidError": ("invite_invalid", "Ссылка-приглашение Telegram недействительна"),
    "InviteRequestSentError": ("join_requested", "Заявка на вступление отправлена и ожидает одобрения"),
    "ChannelsTooMuchError": ("channel_limit_reached", "Аккаунт достиг лимита каналов и групп Telegram"),
    "UserChannelsTooMuchError": ("peer_channel_limit_reached", "Пользователь достиг лимита каналов и групп Telegram"),
    "UsersTooMuchError": ("chat_member_limit_reached", "В группе достигнут лимит участников"),
    # Permissions/privacy.
    "ChatAdminRequiredError": ("admin_required", "Для этого действия требуются права администратора"),
    "ChatAdminInviteRequiredError": ("admin_invite_required", "Вступление возможно только по приглашению администратора"),
    "ChatWriteForbiddenError": ("chat_write_forbidden", "Telegram запретил отправку сообщений в этот чат"),
    "ChatSendPlainForbiddenError": ("plain_text_forbidden", "В этом чате запрещены обычные текстовые сообщения"),
    "ChatRestrictedError": ("chat_restricted", "Telegram ограничил действия в этом чате"),
    "RightForbiddenError": ("right_forbidden", "У аккаунта нет требуемого права Telegram"),
    "UserPrivacyRestrictedError": ("privacy_restricted", "Настройки приватности получателя запрещают это действие"),
    "UserIsBlockedError": ("user_blocked", "Получатель заблокировал аккаунт"),
    "YouBlockedUserError": ("user_blocked", "Получатель находится в списке заблокированных"),
    "BotGroupsBlockedError": ("bot_groups_blocked", "Боту запрещено добавление в группы"),
    # Messages/replies/topics.
    "RandomIdDuplicateError": ("message_random_id_duplicate", "Telegram уже принял сообщение с этим идентификатором отправки"),
    "MessageEmptyError": ("message_empty", "Telegram отклонил пустое сообщение"),
    "MessageTooLongError": ("message_too_long", "Сообщение превышает допустимую длину Telegram"),
    "EntityBoundsInvalidError": ("entity_bounds_invalid", "Разметка сообщения содержит неверные границы"),
    "EntitiesTooLongError": ("entities_too_long", "Разметка сообщения слишком велика"),
    "MediaCaptionTooLongError": ("caption_too_long", "Подпись к медиа слишком длинная"),
    "MessageIdInvalidError": ("message_id_invalid", "Telegram не нашёл указанное сообщение"),
    "MsgIdInvalidError": ("message_id_invalid", "Telegram не принял идентификатор сообщения"),
    "MessageNotModifiedError": ("message_not_modified", "Данные сообщения уже имеют это значение"),
    "MessageAuthorRequiredError": ("message_author_required", "Действие доступно только автору сообщения"),
    "MessageEditTimeExpiredError": ("message_edit_time_expired", "Срок изменения сообщения истёк"),
    "MessageDeleteForbiddenError": ("message_delete_forbidden", "Удаление этого сообщения запрещено"),
    "ReplyMarkupInvalidError": ("reply_markup_invalid", "Клавиатура или разметка ответа недействительна"),
    "ReplyMarkupTooLongError": ("reply_markup_too_long", "Клавиатура или разметка ответа слишком велика"),
    "ReplyMessageIdInvalidError": ("reply_message_invalid", "Сообщение для ответа не найдено"),
    "TopicClosedError": ("topic_closed", "Тема форума закрыта"),
    "TopicDeletedError": ("topic_deleted", "Тема форума удалена"),
    "ForumTopicDeletedError": ("topic_deleted", "Тема форума удалена"),
    "ScheduleDateTooLateError": ("schedule_date_too_late", "Дата отложенной отправки слишком далека"),
    "ScheduleTooMuchError": ("schedule_limit_reached", "Достигнут лимит отложенных сообщений"),
    "ScheduleBotNotAllowedError": ("schedule_bot_not_allowed", "Ботам недоступна отложенная отправка"),
    "SlowModeMultiMsgsDisabledError": ("slow_mode_multiple_forbidden", "Медленный режим запрещает несколько сообщений подряд"),
    "ChatDiscussionUnallowedError": ("comments_disabled", "Комментарии к публикации отключены"),
    # Reactions.
    "ReactionInvalidError": ("reaction_invalid", "Эта реакция недоступна для сообщения"),
    "ReactionEmptyError": ("reaction_empty", "Telegram не получил выбранную реакцию"),
    "ReactionTooManyError": ("reaction_limit_reached", "Для сообщения достигнут лимит реакций аккаунта"),
    "ReactionsTooManyError": ("reaction_limit_reached", "Для сообщения достигнут лимит реакций"),
    "ChatReactionsNoneError": ("reactions_disabled", "Реакции в этом чате отключены"),
    "ChatReactionsSomeError": ("reaction_not_allowed", "Выбранная реакция не разрешена в этом чате"),
    "ReactionInvalidEmojiError": ("reaction_invalid", "Telegram не поддерживает выбранную реакцию"),
    # Contacts/phone.
    "PhoneNumberInvalidError": ("phone_invalid", "Номер телефона имеет неверный формат"),
    "PhoneNumberBannedError": ("phone_banned", "Номер телефона заблокирован Telegram"),
    "PhoneNumberOccupiedError": ("phone_occupied", "Номер телефона уже используется"),
    "PhoneNumberFloodError": ("phone_flood", "Telegram временно ограничил операции с этим номером"),
    "ContactIdInvalidError": ("contact_invalid", "Контакт Telegram недействителен"),
    "ContactNameEmptyError": ("contact_name_empty", "Имя контакта не заполнено"),
    "ContactAddMissingError": ("contact_add_missing", "Telegram не разрешил добавить этот контакт"),
    # Misc deterministic request failures.
    "FileReferenceExpiredError": ("file_reference_expired", "Ссылка Telegram на файл истекла"),
    "MediaInvalidError": ("media_invalid", "Telegram отклонил вложение"),
    "MediaEmptyError": ("media_empty", "Вложение отсутствует"),
    "PollClosedError": ("poll_closed", "Опрос уже закрыт"),
    "PollOptionInvalidError": ("poll_option_invalid", "Вариант ответа опроса недействителен"),
}

_TEXT_RESULTS: dict[str, tuple[str, str]] = {
    "AUTH_KEY_UNREGISTERED": ("authorization_required", "Сессия Telegram больше не зарегистрирована"),
    "SESSION_REVOKED": ("authorization_required", "Сессия Telegram отозвана"),
    "SESSION_EXPIRED": ("authorization_required", "Сессия Telegram истекла"),
    "USER_RESTRICTED": ("user_restricted", "Telegram ограничил действия этого аккаунта"),
    "PEER_ID_INVALID": ("peer_invalid", "Telegram не распознал получателя или чат"),
    "USERNAME_INVALID": ("username_invalid", "Имя пользователя или группы Telegram имеет неверный формат"),
    "USERNAME_NOT_OCCUPIED": ("username_not_found", "Имя пользователя или группы Telegram не найдено"),
    "CHANNEL_PRIVATE": ("channel_private", "Группа или канал закрыты либо недоступны аккаунту"),
    "USER_NOT_PARTICIPANT": ("join_required", "Аккаунт не является участником группы"),
    "USER_BANNED_IN_CHANNEL": ("user_banned", "Аккаунт заблокирован в этой группе или канале"),
    "INVITE_HASH_EXPIRED": ("invite_expired", "Ссылка-приглашение Telegram истекла"),
    "INVITE_HASH_INVALID": ("invite_invalid", "Ссылка-приглашение Telegram недействительна"),
    "INVITE_REQUEST_SENT": ("join_requested", "Заявка на вступление отправлена и ожидает одобрения"),
    "CHAT_ADMIN_REQUIRED": ("admin_required", "Для этого действия требуются права администратора"),
    "CHAT_WRITE_FORBIDDEN": ("chat_write_forbidden", "Telegram запретил отправку сообщений в этот чат"),
    "CHAT_SEND_PLAIN_FORBIDDEN": ("plain_text_forbidden", "В этом чате запрещены обычные текстовые сообщения"),
    "USER_PRIVACY_RESTRICTED": ("privacy_restricted", "Настройки приватности получателя запрещают это действие"),
    "RANDOM_ID_DUPLICATE": ("message_random_id_duplicate", "Telegram уже принял сообщение с этим идентификатором отправки"),
    "MESSAGE_EMPTY": ("message_empty", "Telegram отклонил пустое сообщение"),
    "MESSAGE_TOO_LONG": ("message_too_long", "Сообщение превышает допустимую длину Telegram"),
    "MESSAGE_ID_INVALID": ("message_id_invalid", "Telegram не нашёл указанное сообщение"),
    "REACTION_INVALID": ("reaction_invalid", "Эта реакция недоступна для сообщения"),
    "CHAT_REACTIONS_NONE": ("reactions_disabled", "Реакции в этом чате отключены"),
    "PHONE_NUMBER_INVALID": ("phone_invalid", "Номер телефона имеет неверный формат"),
    "PHONE_NUMBER_BANNED": ("phone_banned", "Номер телефона заблокирован Telegram"),
}

_TRANSIENT_NAMES = frozenset({
    "ServerError",
    "TimedOutError",
    "InterdcCallErrorError",
    "InterdcCallRichErrorError",
    "RpcMcgetFailError",
    "WorkerBusyTooLongRetryError",
})

_WAIT_RE = re.compile(r"(?:FLOOD_WAIT|SLOWMODE_WAIT|SLOW_MODE_WAIT)_?(\d+)", re.IGNORECASE)


def _deferred(code: str, message: str, retry_after: int) -> TelegramRpcResult:
    return TelegramRpcResult(
        TelegramResultDisposition.DEFERRED,
        code,
        message,
        max(1, int(retry_after)),
    )


def classify_telegram_rpc_error(
    *,
    rpc_code: int,
    rpc_name: str,
    rpc_text: str,
    request_dispatched: bool,
    retry_network: bool,
    wait_seconds: int | None = None,
) -> TelegramRpcResult:
    """Return a stable result for every Telegram RPCError.

    Unrecognized class names still get a deterministic result based on the RPC
    status code. Only a transient/server failure after a mutating request crossed
    the dispatch boundary remains uncertain, because Telegram may have committed
    it while the confirmation was lost.
    """

    code = int(rpc_code or 0)
    name = str(rpc_name or "RPCError")
    text = str(rpc_text or "")
    upper = text.upper()

    exact = _EXACT_RESULTS.get(name)
    if exact is not None:
        stable_code, message = exact
        return TelegramRpcResult(TelegramResultDisposition.FAILED, stable_code, message)

    for token, (stable_code, message) in _TEXT_RESULTS.items():
        if token in upper:
            return TelegramRpcResult(TelegramResultDisposition.FAILED, stable_code, message)

    extracted_wait = int(wait_seconds or 0)
    if extracted_wait <= 0:
        match = _WAIT_RE.search(upper)
        if match:
            extracted_wait = int(match.group(1))
    if code == 420 or "FLOOD" in name.upper() or "FLOOD_WAIT" in upper:
        if extracted_wait > 0:
            return _deferred(
                "flood_wait_deferred",
                f"Telegram ограничил частоту действий; продолжение через {extracted_wait} сек",
                extracted_wait,
            )
        return TelegramRpcResult(
            TelegramResultDisposition.FAILED,
            "peer_flood",
            "Telegram ограничил активность аккаунта без срока автоматического продолжения",
        )
    if "SLOWMODE" in name.upper() or "SLOW_MODE" in upper:
        return _deferred(
            "slow_mode_wait_deferred",
            "В чате действует медленный режим Telegram",
            extracted_wait or 60,
        )

    if code >= 500 or name in _TRANSIENT_NAMES:
        if request_dispatched and not retry_network:
            return TelegramRpcResult(
                TelegramResultDisposition.UNCERTAIN,
                "telegram_confirmation_lost_after_dispatch",
                "Запрос передан Telegram, но подтверждение результата потеряно из-за временного сбоя",
            )
        return _deferred(
            "telegram_rpc_deferred",
            "Временный сбой сервера Telegram; задача будет продолжена позже",
            45,
        )

    if code == 303:
        return _deferred(
            "telegram_dc_migration_deferred",
            "Telegram перенаправил запрос в другой дата-центр; соединение будет создано заново",
            3,
        )
    if code == 401:
        return TelegramRpcResult(
            TelegramResultDisposition.FAILED,
            "authorization_required",
            "Telegram требует повторной авторизации аккаунта",
        )
    if code == 403:
        return TelegramRpcResult(
            TelegramResultDisposition.FAILED,
            "telegram_forbidden",
            "Telegram запретил это действие для аккаунта или чата",
        )
    if code == 404:
        return TelegramRpcResult(
            TelegramResultDisposition.FAILED,
            "telegram_not_found",
            "Запрошенный объект Telegram не найден",
        )
    if code == 406:
        return TelegramRpcResult(
            TelegramResultDisposition.FAILED,
            "telegram_auth_key_rejected",
            "Telegram отклонил ключ авторизации или версию протокола",
        )
    if code == 400:
        return TelegramRpcResult(
            TelegramResultDisposition.FAILED,
            "telegram_bad_request",
            "Telegram отклонил параметры запроса",
        )

    suffix = str(abs(code)) if code else "unclassified"
    return TelegramRpcResult(
        TelegramResultDisposition.FAILED,
        f"telegram_rpc_{suffix}",
        "Telegram отклонил запрос",
    )
