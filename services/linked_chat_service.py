from __future__ import annotations

import logging

from services.telegram_service import TelegramService

log = logging.getLogger(__name__)


class LinkedChatService:
    """Resolve linked discussion chat identifiers without membership probes."""

    def __init__(self, telegram: TelegramService):
        self.telegram = telegram

    async def get_linked_chat_id(self, channel_id, *, dispatch_barrier=None):
        linked_chat_id = await self.telegram.get_linked_chat(
            channel_id,
            dispatch_barrier=dispatch_barrier,
        )
        if linked_chat_id is None:
            log.info("Channel %s has no linked discussion chat", channel_id)
        return linked_chat_id
