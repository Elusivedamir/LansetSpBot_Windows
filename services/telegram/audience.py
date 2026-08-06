from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping
from urllib.parse import urlparse

from telethon import types, utils
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, GetFullChatRequest

from core.exceptions import NonRetryableTelegramError

if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:
    class _MixinHost:
        pass


class TelegramAudienceMixin(_MixinHost):
    """Read-only participant enumeration for one accessible Telegram group."""

    @staticmethod
    def _invite_hash(value: str) -> str | None:
        clean = str(value or "").strip()
        if not clean:
            return None
        parsed = urlparse(clean if "://" in clean else f"https://{clean}")
        host = parsed.netloc.lower().removeprefix("www.")
        if host not in {"t.me", "telegram.me"}:
            return None
        path = parsed.path.strip("/")
        if path.startswith("+") and len(path) > 1:
            return path[1:]
        if path.startswith("joinchat/") and len(path) > len("joinchat/"):
            return path.split("/", 1)[1]
        return None

    @staticmethod
    def _audience_input_peer(source: Mapping[str, Any]):
        link = str(source.get("link") or "").strip()
        if link:
            return link
        peer_id = int(source.get("peer_id") or 0)
        peer_type = str(source.get("peer_type") or "").strip().lower()
        real_id, _peer_class = utils.resolve_id(peer_id)
        if peer_type == "chat":
            return types.InputPeerChat(real_id)
        if peer_type == "channel":
            access_hash = source.get("access_hash")
            if access_hash is None:
                return peer_id
            return types.InputPeerChannel(real_id, int(access_hash))
        raise NonRetryableTelegramError(
            "Не удалось определить тип выбранной группы",
            code="invalid_audience_source",
        )

    async def resolve_audience_group(
        self, source: Mapping[str, Any], *, dispatch_barrier=None
    ):
        await self.ensure_connected()
        link = str(source.get("link") or "").strip()
        invite_hash = self._invite_hash(link)
        try:
            if invite_hash:
                invite = await self.execute(
                    self.client,
                    CheckChatInviteRequest(invite_hash),
                    retry_network=True,
                    dispatch_barrier=dispatch_barrier,
                )
                if not isinstance(invite, types.ChatInviteAlready):
                    raise NonRetryableTelegramError(
                        "Выбранный аккаунт не состоит в этой приватной группе",
                        code="audience_membership_required",
                    )
                entity = invite.chat
            else:
                entity = await self.execute(
                    self.client.get_entity,
                    self._audience_input_peer(source),
                    retry_network=True,
                    dispatch_barrier=dispatch_barrier,
                )
        except ChannelPrivateError as exc:
            raise NonRetryableTelegramError(
                "Группа недоступна выбранному аккаунту",
                code="audience_group_inaccessible",
            ) from exc
        except NonRetryableTelegramError as exc:
            if str(getattr(exc, "code", "")) == "channel_private":
                raise NonRetryableTelegramError(
                    "Группа недоступна выбранному аккаунту",
                    code="audience_group_inaccessible",
                ) from exc
            raise

        if bool(getattr(entity, "broadcast", False)):
            raise NonRetryableTelegramError(
                "Парсинг участников каналов не поддерживается. Выберите группу или супергруппу",
                code="audience_channel_not_supported",
            )
        is_supergroup = bool(getattr(entity, "megagroup", False))
        is_basic_group = isinstance(entity, types.Chat)
        if not (is_supergroup or is_basic_group):
            raise NonRetryableTelegramError(
                "Выбранный источник не является группой",
                code="audience_group_required",
            )
        if bool(getattr(entity, "left", False)) or bool(getattr(entity, "deactivated", False)):
            raise NonRetryableTelegramError(
                "Выбранный аккаунт больше не состоит в этой группе",
                code="audience_membership_required",
            )
        return entity

    async def iter_audience_member_pages(
        self,
        entity: Any,
        *,
        offset: int = 0,
        page_size: int = 200,
        dispatch_barrier=None,
    ) -> AsyncIterator[tuple[int, list[tuple[Any, bool]]]]:
        """Yield stable Telegram pages and their next offset for crash-safe resume."""

        await self.ensure_connected()
        current = max(0, int(offset or 0))
        size = max(1, min(200, int(page_size or 200)))
        try:
            if isinstance(entity, types.Chat):
                full = await self.execute(
                    self.client,
                    GetFullChatRequest(chat_id=int(entity.id)),
                    retry_network=True,
                    dispatch_barrier=dispatch_barrier,
                )
                participants_obj = getattr(getattr(full, "full_chat", None), "participants", None)
                participants = list(getattr(participants_obj, "participants", None) or [])
                users_by_id = {
                    int(getattr(user, "id", 0) or 0): user
                    for user in list(getattr(full, "users", None) or [])
                }
                ordered: list[tuple[Any, bool]] = []
                for participant in participants:
                    user_id = int(getattr(participant, "user_id", 0) or 0)
                    user = users_by_id.get(user_id)
                    if user is None:
                        continue
                    is_admin = isinstance(
                        participant,
                        (types.ChatParticipantAdmin, types.ChatParticipantCreator),
                    )
                    ordered.append((user, is_admin))
                while current < len(ordered):
                    page = ordered[current : current + size]
                    current += len(page)
                    yield current, page
                return

            input_channel = utils.get_input_channel(entity)
            while True:
                result = await self.execute(
                    self.client,
                    GetParticipantsRequest(
                        channel=input_channel,
                        filter=types.ChannelParticipantsSearch(""),
                        offset=current,
                        limit=size,
                        hash=0,
                    ),
                    retry_network=True,
                    dispatch_barrier=dispatch_barrier,
                )
                participants = list(getattr(result, "participants", None) or [])
                users_by_id = {
                    int(getattr(user, "id", 0) or 0): user
                    for user in list(getattr(result, "users", None) or [])
                }
                page: list[tuple[Any, bool]] = []
                for participant in participants:
                    user_id = int(getattr(participant, "user_id", 0) or 0)
                    user = users_by_id.get(user_id)
                    if user is None:
                        continue
                    is_admin = isinstance(
                        participant,
                        (types.ChannelParticipantAdmin, types.ChannelParticipantCreator),
                    )
                    page.append((user, is_admin))
                if not participants:
                    break
                current += len(participants)
                yield current, page
                if len(participants) < size:
                    break
        except ChatAdminRequiredError as exc:
            raise NonRetryableTelegramError(
                "Telegram скрыл список участников этой группы для выбранного аккаунта",
                code="audience_members_hidden",
            ) from exc
        except ChannelPrivateError as exc:
            raise NonRetryableTelegramError(
                "Группа недоступна выбранному аккаунту",
                code="audience_group_inaccessible",
            ) from exc

    async def iter_audience_members(
        self, entity: Any, *, dispatch_barrier=None
    ) -> AsyncIterator[Any]:
        """Compatibility stream preserving the historical public method."""

        async for _next_offset, page in self.iter_audience_member_pages(
            entity,
            offset=0,
            page_size=200,
            dispatch_barrier=dispatch_barrier,
        ):
            for user, _is_admin in page:
                yield user
