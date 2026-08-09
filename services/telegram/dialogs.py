from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from telethon import functions, types, utils

from core.exceptions import NonRetryableTelegramError, TelegramOperationError

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

    @staticmethod
    def _dialog_sync_state_record(state) -> dict[str, int]:
        return {
            "version": 1,
            "pts": int(getattr(state, "pts", 0) or 0),
            "qts": int(getattr(state, "qts", 0) or 0),
            "date": int(getattr(state, "date").timestamp()),
            "seq": int(getattr(state, "seq", 0) or 0),
        }

    @staticmethod
    def _validated_dialog_sync_state(value: object) -> dict[str, int]:
        if not isinstance(value, dict) or int(value.get("version") or 0) != 1:
            raise NonRetryableTelegramError(
                "Incremental channel state is missing or incompatible; run full synchronization",
                code="full_sync_required",
            )
        try:
            result = {
                "version": 1,
                "pts": int(value["pts"]),
                "qts": int(value["qts"]),
                "date": int(value["date"]),
                "seq": int(value.get("seq") or 0),
            }
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise NonRetryableTelegramError(
                "Incremental channel state is invalid; run full synchronization",
                code="full_sync_required",
            ) from exc
        if result["pts"] < 0 or result["qts"] < 0 or result["date"] <= 0:
            raise NonRetryableTelegramError(
                "Incremental channel state is invalid; run full synchronization",
                code="full_sync_required",
            )
        return result

    def _snapshot_from_difference_entity(self, entity) -> dict[str, Any] | None:
        if isinstance(entity, types.Channel):
            if bool(getattr(entity, "left", False)):
                return None
            dialog = SimpleNamespace(
                is_channel=True,
                is_group=bool(getattr(entity, "megagroup", False)),
            )
        elif isinstance(entity, types.Chat):
            if bool(getattr(entity, "left", False)) or bool(
                getattr(entity, "deactivated", False)
            ):
                return None
            dialog = SimpleNamespace(is_channel=False, is_group=True)
        else:
            return None
        try:
            self.register_peer_reference(utils.get_peer_id(entity), entity=entity)
        except Exception:
            log.debug("Could not cache incremental dialog InputPeer", exc_info=True)
        work_target = self._work_target_row(dialog, entity)
        saved_dialog = self._saved_dialog_row(dialog, entity)
        if work_target is None and saved_dialog is None:
            return None
        return {"work_target": work_target, "saved_dialog": saved_dialog}

    async def get_dialog_sync_state(self) -> dict[str, int]:
        await self.ensure_connected()
        state = await self.execute(
            self.client,
            functions.updates.GetStateRequest(),
            retry_network=True,
        )
        return self._dialog_sync_state_record(state)

    async def fetch_incremental_dialog_snapshots(
        self,
        marker: object,
        *,
        max_slices: int = 64,
        pts_total_limit: int = 10_000,
    ) -> dict[str, Any]:
        await self.ensure_connected()
        current = self._validated_dialog_sync_state(marker)
        snapshots: dict[int, dict[str, Any]] = {}
        for _slice_index in range(max(1, int(max_slices))):
            request = functions.updates.GetDifferenceRequest(
                pts=current["pts"],
                date=datetime.fromtimestamp(current["date"], tz=timezone.utc),
                qts=current["qts"],
                pts_total_limit=max(1, int(pts_total_limit)),
            )
            difference = await self.execute(
                self.client,
                request,
                retry_network=True,
            )
            if isinstance(difference, types.updates.DifferenceTooLong):
                raise NonRetryableTelegramError(
                    "Telegram update gap is too large for incremental synchronization; run full synchronization",
                    code="full_sync_required",
                    details={"remote_pts": int(getattr(difference, "pts", 0) or 0)},
                )
            for entity in list(getattr(difference, "chats", None) or []):
                snapshot = self._snapshot_from_difference_entity(entity)
                if snapshot is None:
                    continue
                peer = snapshot.get("saved_dialog") or snapshot.get("work_target") or {}
                peer_id = peer.get("peer_id", peer.get("id"))
                if peer_id is not None:
                    snapshots[int(peer_id)] = snapshot
            if isinstance(difference, types.updates.DifferenceEmpty):
                current = {
                    **current,
                    "date": int(difference.date.timestamp()),
                    "seq": int(difference.seq),
                }
                return {"snapshots": list(snapshots.values()), "state": current}
            if isinstance(difference, types.updates.Difference):
                current = self._dialog_sync_state_record(difference.state)
                return {"snapshots": list(snapshots.values()), "state": current}
            if isinstance(difference, types.updates.DifferenceSlice):
                current = self._dialog_sync_state_record(
                    difference.intermediate_state
                )
                continue
            raise NonRetryableTelegramError(
                f"Unsupported Telegram update difference: {type(difference).__name__}",
                code="full_sync_required",
            )
        raise NonRetryableTelegramError(
            "Telegram returned too many update slices for safe incremental synchronization; run full synchronization",
            code="full_sync_required",
        )

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
