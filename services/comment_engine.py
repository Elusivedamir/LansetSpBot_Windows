from __future__ import annotations

import secrets

from core.config import DEFAULT_MAX_CHANNELS_PER_RUN


class CommentEngine:
    def __init__(self, daily_limit=DEFAULT_MAX_CHANNELS_PER_RUN):
        self.daily_limit = daily_limit
        self.last_comment = None
        self._random = secrets.SystemRandom()

    def random_comment(self, templates):
        items = [item for item in templates if item]
        if not items:
            return None
        choices = [item for item in items if item != self.last_comment] or items
        self.last_comment = self._random.choice(choices)
        return self.last_comment

    def random_delay(self, delay_min, delay_max):
        low, high = sorted((int(delay_min), int(delay_max)))
        return self._random.randint(low, high)

    def can_send(self, sent_count):
        return int(sent_count) < int(self.daily_limit)

    async def process(self, task, comment_service, templates):
        text = self.random_comment(templates)
        if not text:
            raise ValueError("No comment template")
        channel_id = task.get("channel_id")
        if channel_id is None:
            raise ValueError("channel_id is required for a channel-post comment")
        return await comment_service.send_comment(
            channel_id=channel_id,
            linked_chat_id=task.get("linked_chat_id"),
            post_message_id=task["post_id"],
            text=text,
            reply_to=task.get("reply_to"),
        )
