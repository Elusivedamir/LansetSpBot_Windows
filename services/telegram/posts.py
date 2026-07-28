from __future__ import annotations

from typing import TYPE_CHECKING, Any

import logging
from contextlib import nullcontext
from telethon import functions, types, utils

from core.exceptions import (
    NonRetryableTelegramError,
)

from services.telegram.models import LatestPostResult

log = logging.getLogger(__name__)

# The first pass must cover the three newest publications.
#
# The window is measured in history rows, not publications: one publication can
# occupy up to ten rows, because that is the largest album Telegram allows. Three
# publications are therefore thirty rows.
#
# Thirty rows still cost exactly one ``messages.GetHistory`` round trip, since a
# single request returns up to a hundred messages. Narrowing the window does not
# save a request; it only makes the batch saturate, and a saturated batch is what
# forces the second, expanded request below. The previous window of four rows
# saturated on any album of four or more media and on any run of four service
# events, and paid a second full history request for the same answer.
LATEST_POST_SCAN_POSTS = 3
MAX_ALBUM_MESSAGES = 10
LATEST_POST_SCAN_LIMIT = LATEST_POST_SCAN_POSTS * MAX_ALBUM_MESSAGES
# Bounded fallback for pathological feeds (long uninterrupted service-event
# runs). The scan still returns as soon as the next real publication is found.
EXPANDED_POST_SCAN_LIMIT = 1000
# A single history request cannot return more than this many messages.
MAX_HISTORY_PAGE = 100


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class TelegramPostResolverMixin(_MixinHost):
    @staticmethod
    def _raw_peer_id(value) -> int | None:
        """Return Telegram's unmarked numeric peer id for reliable comparisons."""
        if value is None:
            return None
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return None
        if numeric >= 0:
            return numeric
        try:
            return int(utils.resolve_id(numeric)[0])
        except Exception:
            return abs(numeric)

    async def _resolve_post_discussion(
        self,
        *,
        channel_ref,
        channel_id,
        message,
        dispatch_barrier=None,
    ) -> LatestPostResult:
        """Resolve the current linked discussion/root for one exact source post."""

        try:
            discussion = await self.execute(
                self.client,
                functions.messages.GetDiscussionMessageRequest(
                    peer=channel_ref,
                    msg_id=int(message.id),
                ),
                dispatch_barrier=dispatch_barrier,
            )
        except NonRetryableTelegramError as exc:
            code = getattr(exc, "code", "")
            if code == "comments_disabled":
                return LatestPostResult("comments_disabled", message)
            if code == "message_id_invalid":
                return LatestPostResult("discussion_missing", message)
            raise

        discussion_messages = list(getattr(discussion, "messages", None) or [])
        if not discussion_messages:
            return LatestPostResult("discussion_missing", message)

        source_raw_id = self._raw_peer_id(channel_id)
        discussion_chat_id: int | None = None
        for chat in list(getattr(discussion, "chats", None) or []):
            try:
                marked_candidate = int(utils.get_peer_id(chat))
            except Exception:
                raw_candidate = self._raw_peer_id(getattr(chat, "id", None))
                marked_candidate = (
                    int(utils.get_peer_id(types.PeerChannel(raw_candidate)))
                    if raw_candidate is not None
                    else 0
                )
            candidate_raw = self._raw_peer_id(marked_candidate)
            if candidate_raw is not None and candidate_raw != source_raw_id:
                discussion_chat_id = marked_candidate
                break

        root_message = None
        discussion_raw_id = self._raw_peer_id(discussion_chat_id)
        if discussion_raw_id is not None:
            for candidate_message in reversed(discussion_messages):
                peer = getattr(candidate_message, "peer_id", None)
                try:
                    marked_peer = (
                        int(utils.get_peer_id(peer)) if peer is not None else None
                    )
                except Exception:
                    marked_peer = None
                if self._raw_peer_id(marked_peer) == discussion_raw_id:
                    root_message = candidate_message
                    break

        if root_message is None:
            for candidate_message in reversed(discussion_messages):
                peer = getattr(candidate_message, "peer_id", None)
                try:
                    marked_peer = (
                        int(utils.get_peer_id(peer)) if peer is not None else None
                    )
                except Exception:
                    marked_peer = None
                candidate_peer_raw = self._raw_peer_id(marked_peer)
                if (
                    candidate_peer_raw is not None
                    and candidate_peer_raw != source_raw_id
                ):
                    root_message = candidate_message
                    discussion_chat_id = discussion_chat_id or marked_peer
                    break

        if root_message is None or discussion_chat_id is None:
            return LatestPostResult("discussion_missing", message)

        discussion_message_id = getattr(root_message, "id", None)
        root_peer = getattr(root_message, "peer_id", None)
        try:
            marked_root_peer = (
                int(utils.get_peer_id(root_peer)) if root_peer is not None else None
            )
        except Exception:
            marked_root_peer = None
        if discussion_message_id is None or self._raw_peer_id(
            marked_root_peer
        ) != self._raw_peer_id(discussion_chat_id):
            return LatestPostResult("discussion_missing", message)

        return LatestPostResult(
            "ok",
            message,
            discussion_chat_id=discussion_chat_id,
            discussion_message_id=int(discussion_message_id),
        )

    async def get_latest_post_for_commenting(
        self,
        channel_id,
        limit: int = LATEST_POST_SCAN_LIMIT,
        *,
        dispatch_barrier=None,
    ) -> LatestPostResult:
        """Resolve the newest publication and its current discussion route.

        The normal path inspects one history page window. A saturated batch is
        ambiguous when every row is a service event or belongs to the same album,
        so in those two cases continue with a bounded expanded scan.  This keeps
        the common RPC path at a single request without treating service events as
        an empty channel or deriving an album's durable post id from a truncated
        prefix.
        """

        await self.ensure_connected()
        channel_ref = self._resolve_peer_reference(channel_id)
        fetch_limit = max(
            1, min(MAX_HISTORY_PAGE, int(limit or LATEST_POST_SCAN_LIMIT))
        )

        scanned_total = 0
        newest: Any | None = None
        grouped_id: Any | None = None
        album_items: list[Any] = []
        last_message_id: int | None = None

        async def scan_messages(scan_limit: int, *, offset_id: int | None = None):
            nonlocal scanned_total, newest, grouped_id, last_message_id
            iterator_kwargs = {"limit": max(1, int(scan_limit))}
            if offset_id is not None:
                # Telethon excludes offset_id itself and continues with older
                # rows, so the expanded scan does not re-read the first page.
                iterator_kwargs["offset_id"] = int(offset_id)
            iterator = self.client.iter_messages(
                channel_ref, **iterator_kwargs
            ).__aiter__()
            scanned_this_pass = 0
            observer = getattr(self.client, "observe_requests", None)
            observer_context = nullcontext()
            if dispatch_barrier is not None and callable(observer):
                observer_context = observer(
                    lambda request: dispatch_barrier.dispatch(request)
                )
            with observer_context:
                async for candidate in self._iter_with_timeout(
                    iterator,
                    dispatch_barrier=(
                        None if callable(observer) else dispatch_barrier
                    ),
                ):
                    scanned_this_pass += 1
                    scanned_total += 1
                    candidate_id = getattr(candidate, "id", None)
                    if candidate_id is not None:
                        last_message_id = int(candidate_id)
                    if (
                        getattr(candidate, "action", None) is not None
                        or candidate_id is None
                    ):
                        continue
                    if newest is None:
                        newest = candidate
                        grouped_id = getattr(candidate, "grouped_id", None)
                        if grouped_id is None:
                            return candidate, False
                        album_items.append(candidate)
                        continue
                    if getattr(candidate, "grouped_id", None) == grouped_id:
                        album_items.append(candidate)
                        continue
                    # The next real publication proves that the newest album is
                    # complete; service events between publications are ignored.
                    return min(
                        album_items,
                        key=lambda item: int(item.id),
                    ), False

            if newest is None:
                return None, scanned_this_pass >= scan_limit
            message = min(
                album_items,
                key=lambda item: int(item.id),
            )
            return message, scanned_this_pass >= scan_limit

        message, needs_expanded_scan = await scan_messages(fetch_limit)
        if needs_expanded_scan:
            # Telegram albums are small in normal operation, while this higher
            # ceiling also tolerates long runs of service events without an
            # unbounded history scan on malformed or service-only channels.
            # Continue after the last inspected row instead of fetching the
            # initial page a second time.
            continuation_offset = last_message_id
            remaining_limit = max(
                1,
                EXPANDED_POST_SCAN_LIMIT - scanned_total,
            )
            if continuation_offset is not None and remaining_limit > 0:
                message, _ = await scan_messages(
                    remaining_limit,
                    offset_id=int(continuation_offset),
                )

        if message is None:
            return LatestPostResult("no_post")

        return await self._resolve_post_discussion(
            channel_ref=channel_ref,
            channel_id=channel_id,
            message=message,
            dispatch_barrier=dispatch_barrier,
        )

    async def get_post_for_commenting(
        self,
        channel_id,
        post_id,
        *,
        dispatch_barrier=None,
    ) -> LatestPostResult:
        """Re-resolve one persisted source post before a mutating comment RPC."""

        await self.ensure_connected()
        channel_ref = self._resolve_peer_reference(channel_id)
        message = await self.execute(
            self.client.get_messages,
            channel_ref,
            ids=int(post_id),
            dispatch_barrier=dispatch_barrier,
        )
        if isinstance(message, (list, tuple)):
            message = message[0] if message else None
        if message is None or getattr(message, "id", None) is None:
            return LatestPostResult("discussion_missing")
        if int(message.id) != int(post_id):
            return LatestPostResult("discussion_missing", message)
        return await self._resolve_post_discussion(
            channel_ref=channel_ref,
            channel_id=channel_id,
            message=message,
            dispatch_barrier=dispatch_barrier,
        )

    async def get_latest_commentable_post(
        self, channel_id, limit: int = LATEST_POST_SCAN_LIMIT
    ):
        """Compatibility wrapper; strict logic never falls back to older posts."""
        result = await self.get_latest_post_for_commenting(channel_id, limit=limit)
        return result.message if result.status == "ok" else None

    async def get_messages(self, chat_id, limit=100):
        return await self.execute(self.client.get_messages, chat_id, limit=limit)
