from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping
from urllib.parse import urlparse

from telethon import types, utils
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
from telethon.tl.functions.messages import CheckChatInviteRequest

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
                # Telethon can resolve a marked peer from its encrypted session
                # cache when the dialog was synchronized earlier.
                return peer_id
            return types.InputPeerChannel(real_id, int(access_hash))
        raise NonRetryableTelegramError(
            "Не удалось определить тип выбранной группы",
            code="invalid_audience_source",
        )

    async def resolve_audience_group(self, source: Mapping[str, Any]):
        await self.ensure_connected()
        link = str(source.get("link") or "").strip()
        invite_hash = self._invite_hash(link)
        try:
            if invite_hash:
                invite = await self.execute(
                    self.client,
                    CheckChatInviteRequest(invite_hash),
                    retry_network=True,
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
        if bool(getattr(entity, "left", False)) or bool(
            getattr(entity, "deactivated", False)
        ):
            raise NonRetryableTelegramError(
                "Выбранный аккаунт больше не состоит в этой группе",
                code="audience_membership_required",
            )
        return entity

    async def iter_audience_members(
        self, entity: Any, *, dispatch_barrier=None
    ) -> AsyncIterator[Any]:
        """Yield members while preserving the shared pagination/FloodWait policy."""

        await self.ensure_connected()
        iterator = self.client.iter_participants(entity).__aiter__()
        try:
            async for user in self._iter_with_timeout(
                iterator, dispatch_barrier=dispatch_barrier
            ):
                yield user
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
