from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import logging
from typing import Any

from telethon import utils

from core.exceptions import TelegramOperationError

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class TelegramDialogsMixin(_MixinHost):
    @staticmethod
    def _saved_dialog_row(dialog, entity) -> dict[str, Any] | None:
        is_channel = bool(getattr(dialog, "is_channel", False))
        is_group = bool(getattr(dialog, "is_group", False))
        if not (is_channel or is_group):
            return None
        entity_id = getattr(entity, "id", None)
        if entity_id is None:
            return None
        peer_id = utils.get_peer_id(entity)
        if bool(getattr(entity, "broadcast", False)):
            kind = "channel"
        elif bool(getattr(entity, "megagroup", False)):
            kind = "supergroup"
        else:
            kind = "group"
        access_hash = getattr(entity, "access_hash", None)
        peer_type = (
            "channel"
            if bool(getattr(entity, "broadcast", False))
            or bool(getattr(entity, "megagroup", False))
            else "chat"
        )
        return {
            "peer_id": int(peer_id),
            "title": getattr(entity, "title", None) or "Без названия",
            "username": getattr(entity, "username", None),
            "kind": kind,
            "invite_link": None,
            "access_hash": int(access_hash) if access_hash is not None else None,
            "peer_type": peer_type,
        }

    def _work_target_row(self, dialog, entity) -> dict[str, Any] | None:
        is_broadcast = bool(getattr(entity, "broadcast", False))
        is_group = bool(getattr(dialog, "is_group", False))
        if is_broadcast:
            access_hash = getattr(entity, "access_hash", None)
            return {
                "id": getattr(entity, "id", None),
                "title": getattr(entity, "title", None)
                or getattr(entity, "first_name", None),
                "username": getattr(entity, "username", None),
                "target_kind": "channel",
                "comment_mode": "channel_post",
                "linked_chat_id": None,
                "linked_chat_title": None,
                "link_status": None,
                "access_hash": int(access_hash) if access_hash is not None else None,
                "peer_type": "channel",
            }
        if not is_group:
            return None
        if not self._group_allows_plain_text(entity):
            log.info(
                "Skipping non-writable group target: id=%s title=%s",
                getattr(entity, "id", None),
                getattr(entity, "title", None),
            )
            return None
        peer_id = utils.get_peer_id(entity)
        title = getattr(entity, "title", None) or "Без названия"
        is_linked_discussion = bool(getattr(entity, "has_link", False))
        access_hash = getattr(entity, "access_hash", None)
        peer_type = "channel" if bool(getattr(entity, "megagroup", False)) else "chat"
        return {
            "id": int(peer_id),
            "title": title,
            "username": getattr(entity, "username", None),
            "target_kind": "group",
            "comment_mode": (
                "linked_discussion" if is_linked_discussion else "pending"
            ),
            "linked_chat_id": int(peer_id),
            "linked_chat_title": title,
            "link_status": (
                "Связанное обсуждение · только комментарии к постам"
                if is_linked_discussion
                else "Обычная группа · сообщение без привязки к посту"
            ),
            "access_hash": int(access_hash) if access_hash is not None else None,
            "peer_type": peer_type,
        }

    async def iter_dialog_snapshot(self):
        """Yield work-target and saved-dialog projections from one Telegram pass."""
        await self.ensure_connected()
        iterator = self.client.iter_dialogs().__aiter__()
        try:
            async for dialog in self._iter_with_timeout(iterator):
                entity = dialog.entity
                try:
                    self.register_peer_reference(
                        utils.get_peer_id(entity), entity=entity
                    )
                except Exception:
                    log.debug("Could not cache dialog InputPeer", exc_info=True)
                work_target = self._work_target_row(dialog, entity)
                saved_dialog = self._saved_dialog_row(dialog, entity)
                if work_target is None and saved_dialog is None:
                    continue
                yield {
                    "work_target": work_target,
                    "saved_dialog": saved_dialog,
                }
        except asyncio.TimeoutError as exc:
            self._connected = False
            raise TelegramOperationError(
                "Telegram dialog pagination timed out"
            ) from exc

    async def iter_channels(self):
        """Compatibility projection over the unified dialog snapshot.

        Internal peer reconstruction fields remain available to the unified
        snapshot but are omitted from this historical public projection.
        """
        async for snapshot in self.iter_dialog_snapshot():
            row = snapshot.get("work_target")
            if row is not None:
                result = dict(row)
                result.pop("access_hash", None)
                result.pop("peer_type", None)
                yield result

    async def get_channels(self) -> list[dict[str, Any]]:
        return [channel async for channel in self.iter_channels()]

    async def iter_saved_dialogs(self):
        """Compatibility projection over the unified dialog snapshot."""
        async for snapshot in self.iter_dialog_snapshot():
            row = snapshot.get("saved_dialog")
            if row is not None:
                yield row
