from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from typing import Any

from telethon import functions, types, utils
from core.exceptions import (
    NonRetryableTelegramError,
)

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class TelegramMembershipMixin(_MixinHost):
    @staticmethod
    def is_channel_peer(value: Any) -> bool:
        """Return whether a stored marked ID represents a channel/supergroup."""
        try:
            numeric = int(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if numeric > 0:
            # Historical broadcast-channel rows use the raw positive ID.
            return True
        try:
            _raw_id, peer_type = utils.resolve_id(numeric)
        except Exception:
            return False
        return peer_type is types.PeerChannel

    @staticmethod
    def _channel_peer_reference(value):
        """Return a Telethon-safe fallback reference for a channel/supergroup."""
        if isinstance(value, bool):
            return value
        try:
            numeric = int(value)
        except (TypeError, ValueError, OverflowError):
            return value
        if numeric > 0:
            return int(utils.get_peer_id(types.PeerChannel(numeric)))
        return numeric

    def register_peer_reference(
        self,
        peer_id,
        *,
        access_hash=None,
        peer_type: str | None = None,
        entity=None,
    ):
        """Cache an InputPeer so normal operations do not need ``get_entity``.

        The cache is fed by ``iter_dialogs`` and can also be reconstructed from
        SQLite after restart using ``peer_id + access_hash``.
        """
        references = getattr(self, "_peer_references", None)
        if references is None:
            references = {}
            self._peer_references = references

        input_peer = None
        if entity is not None:
            try:
                input_peer = utils.get_input_peer(entity)
            except Exception:
                input_peer = None
        try:
            numeric = int(peer_id)
        except (TypeError, ValueError, OverflowError):
            return input_peer

        raw_id = numeric
        resolved_type = str(peer_type or "").strip().lower()
        if numeric < 0:
            try:
                raw_id, detected = utils.resolve_id(numeric)
                if not resolved_type:
                    resolved_type = (
                        "channel" if detected is types.PeerChannel else "chat"
                    )
            except Exception:
                raw_id = abs(numeric)
        if input_peer is None:
            try:
                if resolved_type == "channel" and access_hash is not None:
                    input_peer = types.InputPeerChannel(int(raw_id), int(access_hash))
                elif resolved_type == "chat":
                    input_peer = types.InputPeerChat(int(raw_id))
            except (TypeError, ValueError, OverflowError):
                input_peer = None
        if input_peer is None:
            return None

        keys = {numeric, int(raw_id)}
        try:
            keys.add(int(utils.get_peer_id(types.PeerChannel(int(raw_id)))))
        except Exception:
            pass
        for key in keys:
            references[int(key)] = input_peer
        return input_peer

    def _resolve_peer_reference(
        self, value, *, access_hash=None, peer_type: str | None = None
    ):
        try:
            numeric = int(value)
        except (TypeError, ValueError, OverflowError):
            return value
        references = getattr(self, "_peer_references", {})
        cached = references.get(numeric)
        if cached is not None:
            return cached
        reconstructed = self.register_peer_reference(
            numeric, access_hash=access_hash, peer_type=peer_type
        )
        if reconstructed is not None:
            return reconstructed
        return self._channel_peer_reference(numeric)

    @staticmethod
    def _invite_hash(value: str) -> str | None:
        text = str(value or "").strip()
        for marker in (
            "t.me/+",
            "telegram.me/+",
            "t.me/joinchat/",
            "telegram.me/joinchat/",
        ):
            if marker in text:
                return text.split(marker, 1)[1].split("?", 1)[0].strip("/") or None
        return None

    async def join_saved_dialog(
        self,
        *,
        username=None,
        invite_link=None,
        expected_peer_id=None,
        dispatch_barrier=None,
    ) -> bool:
        """Join only after verifying the durable Telegram peer identity."""

        expected = int(expected_peer_id or 0)
        invite_hash = self._invite_hash(invite_link or "")
        public_username = str(username or "").lstrip("@").strip()

        if invite_hash:
            if expected:
                checked = await self.execute(
                    self.client,
                    functions.messages.CheckChatInviteRequest(invite_hash),
                    retry_network=True,
                    dispatch_barrier=dispatch_barrier,
                )
                checked_chat = getattr(checked, "chat", None)
                if checked_chat is None:
                    if public_username:
                        invite_hash = None
                    else:
                        raise NonRetryableTelegramError(
                            "Telegram invite target identity cannot be verified before JOIN",
                            code="join_target_identity_unverifiable",
                        )
                else:
                    resolved = int(utils.get_peer_id(checked_chat))
                    if resolved != expected:
                        raise NonRetryableTelegramError(
                            "Telegram invite now points to a different peer",
                            code="join_target_identity_mismatch",
                            details={
                                "expected_peer_id": expected,
                                "resolved_peer_id": resolved,
                            },
                        )
            if invite_hash:
                execute_kwargs = {
                    "retry_network": False,
                    "unknown_result_code": "join_result_unknown",
                }
                if dispatch_barrier is not None:
                    execute_kwargs["dispatch_barrier"] = dispatch_barrier
                result = await self.execute(
                    self.client,
                    functions.messages.ImportChatInviteRequest(invite_hash),
                    **execute_kwargs,
                )
                return result is not False

        if not public_username:
            raise NonRetryableTelegramError(
                "У сохранённого чата нет публичного username или проверяемой инвайт-ссылки",
                code="join_target_unavailable",
            )

        target = public_username
        if expected:
            target = await self.execute(
                self.client.get_input_entity,
                public_username,
                retry_network=True,
                dispatch_barrier=dispatch_barrier,
            )
            try:
                resolved = int(utils.get_peer_id(target))
            except Exception as exc:
                raise NonRetryableTelegramError(
                    "Telegram peer identity could not be resolved",
                    code="join_target_identity_unverifiable",
                ) from exc
            if resolved != expected:
                raise NonRetryableTelegramError(
                    "Telegram username now belongs to a different peer",
                    code="join_target_identity_mismatch",
                    details={
                        "expected_peer_id": expected,
                        "resolved_peer_id": resolved,
                    },
                )

        if dispatch_barrier is None:
            return await self.join(target)
        return await self.join(target, dispatch_barrier=dispatch_barrier)

    async def get_linked_chat(self, channel, *, dispatch_barrier=None) -> int | None:
        channel_ref = self._resolve_peer_reference(channel)
        execute_kwargs = {}
        if dispatch_barrier is not None:
            execute_kwargs["dispatch_barrier"] = dispatch_barrier
        full = await self.execute(
            self.client,
            functions.channels.GetFullChannelRequest(channel_ref),
            **execute_kwargs,
        )
        linked_chat_id = getattr(
            getattr(full, "full_chat", None), "linked_chat_id", None
        )
        if linked_chat_id is None:
            return None
        # GetFullChannel returns the raw numeric channel id.  Telethon treats an
        # unmarked positive integer as a user, so Telegram calls must receive the
        # marked channel id (-100...).
        return int(utils.get_peer_id(types.PeerChannel(int(linked_chat_id))))

    async def join(self, chat_id, *, dispatch_barrier=None) -> bool:
        """Send exactly one join request without a membership confirmation RPC.

        A lost or ambiguous response is surfaced as ``join_result_unknown``. The
        mutating request is never replayed and no follow-up ``get_permissions``
        request is issued.
        """
        channel_ref = self._resolve_peer_reference(chat_id)
        execute_kwargs = {
            "retry_network": False,
            "unknown_result_code": "join_result_unknown",
        }
        if dispatch_barrier is not None:
            execute_kwargs["dispatch_barrier"] = dispatch_barrier
        result = await self.execute(
            self.client,
            functions.channels.JoinChannelRequest(channel_ref),
            **execute_kwargs,
        )
        return result is not False

    async def join_without_confirmation(
        self, chat_id, *, dispatch_barrier=None
    ) -> bool:
        """Send one join request without a follow-up membership RPC.

        Link discovery intentionally uses this lean variant: if Telegram does not
        return a confirmed result, the target is recorded as unconfirmed and is
        never checked again by the links task.
        """
        if dispatch_barrier is None:
            return await self.join(chat_id)
        return await self.join(chat_id, dispatch_barrier=dispatch_barrier)
