from __future__ import annotations

from typing import TYPE_CHECKING

import logging


from core.exceptions import (
    NonRetryableTelegramError,
)

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class TelegramMessagingMixin(_MixinHost):
    async def send_message(
        self,
        chat_id,
        text,
        reply_to_message_id=None,
        *,
        unknown_result_code="delivery_result_unknown",
        dispatch_barrier=None,
    ):
        if not isinstance(text, str) or not text.strip():
            raise NonRetryableTelegramError(
                "Message text is empty", code="invalid_payload"
            )
        execute_kwargs = {
            "reply_to": reply_to_message_id,
            "retry_network": False,
            "unknown_result_code": unknown_result_code,
        }
        if dispatch_barrier is not None:
            execute_kwargs["dispatch_barrier"] = dispatch_barrier
        return await self.execute(
            self.client.send_message,
            chat_id,
            text.strip(),
            **execute_kwargs,
        )

    async def send_comment(
        self,
        channel_id,
        post_message_id,
        text,
        reply_to=None,
        linked_chat_id=None,
        dispatch_barrier=None,
    ):
        """Send a channel-post comment through the exact resolved discussion root.

        When the discussion chat and its root message are known, direct reply routing
        avoids a second implicit target resolution inside Telethon. The legacy
        ``comment_to`` route remains as a compatibility fallback.
        """
        if not isinstance(text, str) or not text.strip():
            raise NonRetryableTelegramError(
                "Comment text is empty", code="invalid_payload"
            )
        text = text.strip()
        if reply_to is not None:
            target_chat_id = linked_chat_id
            if target_chat_id is None:
                target_chat_id = await self.get_linked_chat(channel_id)
            if target_chat_id is None:
                raise NonRetryableTelegramError(
                    "Channel has no linked discussion chat", code="linked_chat_missing"
                )
            send_kwargs = {"reply_to_message_id": reply_to}
            if dispatch_barrier is not None:
                send_kwargs["dispatch_barrier"] = dispatch_barrier
            return await self.send_message(
                self._resolve_peer_reference(target_chat_id),
                text,
                **send_kwargs,
            )
        execute_kwargs = {
            "comment_to": post_message_id,
            "retry_network": False,
        }
        if dispatch_barrier is not None:
            execute_kwargs["dispatch_barrier"] = dispatch_barrier
        return await self.execute(
            self.client.send_message,
            self._resolve_peer_reference(channel_id),
            text,
            **execute_kwargs,
        )
