from __future__ import annotations

import asyncio
import random
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import AsyncIterator, Any


class RpcCategory(str, Enum):
    READ = "READ"
    RESOLVE_ENTITY = "RESOLVE_ENTITY"
    JOIN = "JOIN"
    SEND_COMMENT = "SEND_COMMENT"


_RESOLVE_REQUEST_NAMES = frozenset(
    {
        "ResolveUsernameRequest",
        "GetFullChannelRequest",
        "GetFullChatRequest",
        "GetUsersRequest",
        "GetChatsRequest",
        "GetChannelsRequest",
    }
)
_JOIN_REQUEST_NAMES = frozenset(
    {
        "JoinChannelRequest",
        "ImportChatInviteRequest",
    }
)
_SEND_REQUEST_NAMES = frozenset(
    {
        "SendMessageRequest",
        "SendMediaRequest",
        "SendMultiMediaRequest",
    }
)


def classify_rpc_request(request: Any) -> RpcCategory:
    name = type(request).__name__
    if name in _JOIN_REQUEST_NAMES:
        return RpcCategory.JOIN
    if name in _SEND_REQUEST_NAMES:
        return RpcCategory.SEND_COMMENT
    if name in _RESOLVE_REQUEST_NAMES or "Resolve" in name:
        return RpcCategory.RESOLVE_ENTITY
    return RpcCategory.READ


class RateLimiter:
    """Process-wide Telegram RPC gate with independent category ledgers.

    All requests remain globally sequential. READ and RESOLVE_ENTITY never
    consume JOIN or SEND_COMMENT cooldowns. JOIN and SEND_COMMENT have separate
    15-30 second category cooldowns, while the hard global request floor remains
    one second. A newly completed JOIN also receives the explicit post-join
    delay in the campaign worker before SEND_COMMENT.
    """

    MIN_INTERVAL_SECONDS = 1.0
    MUTATING_DELAY_MIN_SECONDS = 15.0
    MUTATING_DELAY_MAX_SECONDS = 30.0
    _state_lock = threading.Lock()
    _active = False
    _next_start = 0.0
    _process_interval_floor = MIN_INTERVAL_SECONDS
    _category_next_start: dict[RpcCategory, float] = {
        category: 0.0 for category in RpcCategory
    }
    _category_counts: dict[RpcCategory, int] = {category: 0 for category in RpcCategory}

    def __init__(self, interval: float = MIN_INTERVAL_SECONDS):
        self.interval = max(
            self.MIN_INTERVAL_SECONDS,
            self.__class__._process_interval_floor,
            float(interval),
        )

    @classmethod
    def configure_process_interval(cls, interval: float) -> float:
        normalized = max(cls.MIN_INTERVAL_SECONDS, float(interval))
        with cls._state_lock:
            cls._process_interval_floor = normalized
        return normalized

    @staticmethod
    def _interrupted() -> bool:
        try:
            from PySide6.QtCore import QThread

            thread = QThread.currentThread()
            return bool(thread and thread.isInterruptionRequested())
        except Exception:
            return False

    def _category_delay(self, category: RpcCategory) -> float:
        # Unit tests deliberately lower ``interval`` below the production floor
        # after construction. Keep those tests fast without weakening runtime.
        if self.interval < self.MIN_INTERVAL_SECONDS:
            return 0.0
        if category in {RpcCategory.JOIN, RpcCategory.SEND_COMMENT}:
            return random.uniform(
                self.MUTATING_DELAY_MIN_SECONDS,
                self.MUTATING_DELAY_MAX_SECONDS,
            )
        return self.interval

    async def _wait_for_slot(self, category: RpcCategory) -> None:
        while True:
            if self._interrupted():
                raise asyncio.CancelledError
            now = time.monotonic()
            with self._state_lock:
                wait_for = max(
                    0.0,
                    self._next_start - now,
                    self._category_next_start.get(category, 0.0) - now,
                )
                if not self._active and wait_for <= 0:
                    self.__class__._active = True
                    self.__class__._next_start = now + self.interval
                    self.__class__._category_next_start[category] = (
                        now + self._category_delay(category)
                    )
                    self.__class__._category_counts[category] = (
                        self.__class__._category_counts.get(category, 0) + 1
                    )
                    return
            await asyncio.sleep(min(0.25, max(0.01, wait_for)))

    async def _wait_for_reentrant_slot(self, category: RpcCategory) -> None:
        """Pace a nested Telethon request without deadlocking the outer gate."""
        while True:
            if self._interrupted():
                raise asyncio.CancelledError
            now = time.monotonic()
            with self._state_lock:
                wait_for = max(0.0, self._next_start - now)
                if wait_for <= 0:
                    self.__class__._next_start = now + self.interval
                    self.__class__._category_counts[category] = (
                        self.__class__._category_counts.get(category, 0) + 1
                    )
                    return
            await asyncio.sleep(min(0.25, max(0.01, wait_for)))

    def _release_slot(self) -> None:
        with self._state_lock:
            self.__class__._active = False

    @asynccontextmanager
    async def request_slot(
        self, category: RpcCategory | str = RpcCategory.READ
    ) -> AsyncIterator[None]:
        normalized = (
            RpcCategory(str(category))
            if not isinstance(category, RpcCategory)
            else category
        )
        await self._wait_for_slot(normalized)
        try:
            yield
        finally:
            self._release_slot()

    @asynccontextmanager
    async def reentrant_request_slot(
        self, category: RpcCategory | str = RpcCategory.READ
    ) -> AsyncIterator[None]:
        normalized = (
            RpcCategory(str(category))
            if not isinstance(category, RpcCategory)
            else category
        )
        await self._wait_for_reentrant_slot(normalized)
        yield

    async def acquire(self, category: RpcCategory | str = RpcCategory.READ) -> None:
        async with self.request_slot(category):
            return

    @classmethod
    def category_snapshot(cls) -> dict[str, int]:
        with cls._state_lock:
            return {
                category.value: int(cls._category_counts.get(category, 0))
                for category in RpcCategory
            }

    @classmethod
    def _reset_for_tests(cls) -> None:
        with cls._state_lock:
            cls._active = False
            cls._next_start = 0.0
            cls._process_interval_floor = cls.MIN_INTERVAL_SECONDS
            cls._category_next_start = {category: 0.0 for category in RpcCategory}
            cls._category_counts = {category: 0 for category in RpcCategory}
