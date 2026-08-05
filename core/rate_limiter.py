from __future__ import annotations

import asyncio
import random
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable


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
_JOIN_REQUEST_NAMES = frozenset({"JoinChannelRequest", "ImportChatInviteRequest"})
_SEND_REQUEST_NAMES = frozenset(
    {"SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest"}
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


WaitObserver = Callable[[RpcCategory, float], None]
SleepCallable = Callable[[float], Awaitable[None]]


class RateLimiter:
    """One process-wide non-overlapping Telegram RPC gate.

    Runtime guarantees:
    * every separate Telegram API dispatch is separated by a fresh 2–5 second gap;
    * JOIN dispatches publish one process-wide 2–5 minute cooldown; resumable
      workflows checkpoint the remaining value before sleeping, so parallel
      workers cannot bypass it and the delay is never applied twice;
    * SEND_COMMENT keeps its independent conservative 15–30 second cooldown;
    * local cooldowns are never represented as Telegram FloodWait.

    Tests can inject the random source, clock and sleep function without waiting.
    """

    MIN_INTERVAL_SECONDS = 1.0
    API_DELAY_MIN_SECONDS = 2.0
    API_DELAY_MAX_SECONDS = 5.0
    JOIN_DELAY_MIN_SECONDS = 120.0
    JOIN_DELAY_MAX_SECONDS = 300.0
    SEND_DELAY_MIN_SECONDS = 15.0
    SEND_DELAY_MAX_SECONDS = 30.0

    # Backward-compatible names used by existing tests/configuration.
    MUTATING_DELAY_MIN_SECONDS = SEND_DELAY_MIN_SECONDS
    MUTATING_DELAY_MAX_SECONDS = SEND_DELAY_MAX_SECONDS

    _state_lock = threading.Lock()
    _active = False
    _next_start = 0.0
    _process_interval_floor = MIN_INTERVAL_SECONDS
    _category_next_start: dict[RpcCategory, float] = {
        category: 0.0 for category in RpcCategory
    }
    _category_counts: dict[RpcCategory, int] = {
        category: 0 for category in RpcCategory
    }

    def __init__(
        self,
        interval: float = MIN_INTERVAL_SECONDS,
        *,
        rng: random.Random | Any | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: SleepCallable | None = None,
        wait_observer: WaitObserver | None = None,
    ) -> None:
        self.interval = max(
            self.MIN_INTERVAL_SECONDS,
            self.__class__._process_interval_floor,
            float(interval),
        )
        self._rng = rng or random
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._wait_observer = wait_observer

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

    def _uniform(self, low: float, high: float) -> float:
        return float(self._rng.uniform(float(low), float(high)))

    def _api_delay(self) -> float:
        # Existing unit tests deliberately lower interval after construction.
        if self.interval < self.MIN_INTERVAL_SECONDS:
            return max(0.0, float(self.interval))
        return max(
            self.interval,
            self._uniform(self.API_DELAY_MIN_SECONDS, self.API_DELAY_MAX_SECONDS),
        )

    def _category_delay(self, category: RpcCategory) -> float:
        if self.interval < self.MIN_INTERVAL_SECONDS:
            return 0.0
        if category is RpcCategory.JOIN:
            return self._uniform(
                self.JOIN_DELAY_MIN_SECONDS, self.JOIN_DELAY_MAX_SECONDS
            )
        if category is RpcCategory.SEND_COMMENT:
            return self._uniform(
                self.SEND_DELAY_MIN_SECONDS, self.SEND_DELAY_MAX_SECONDS
            )
        return 0.0

    def _notify_wait(self, category: RpcCategory, seconds: float) -> None:
        observer = self._wait_observer
        if observer is not None and seconds > 0:
            observer(category, seconds)

    async def _cooperative_sleep(self, category: RpcCategory, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        if remaining <= 0:
            return
        self._notify_wait(category, remaining)
        while remaining > 0:
            if self._interrupted():
                raise asyncio.CancelledError
            step = min(0.25, remaining)
            await self._sleep(max(0.0, step))
            remaining -= step

    async def _wait_for_slot(self, category: RpcCategory) -> None:
        while True:
            if self._interrupted():
                raise asyncio.CancelledError
            now = self._monotonic()
            with self._state_lock:
                active = self.__class__._active
                wait_for = max(
                    0.0,
                    self.__class__._next_start - now,
                    self.__class__._category_next_start.get(category, 0.0) - now,
                )
                if not active and wait_for <= 0:
                    self.__class__._active = True
                    self.__class__._category_counts[category] = (
                        self.__class__._category_counts.get(category, 0) + 1
                    )
                    return
            sleep_for = 0.25 if active else max(0.01, wait_for)
            await self._cooperative_sleep(category, sleep_for)

    async def _wait_for_reentrant_slot(self, category: RpcCategory) -> None:
        while True:
            if self._interrupted():
                raise asyncio.CancelledError
            now = self._monotonic()
            with self._state_lock:
                wait_for = max(0.0, self.__class__._next_start - now)
                if wait_for <= 0:
                    self.__class__._category_counts[category] = (
                        self.__class__._category_counts.get(category, 0) + 1
                    )
                    return
            await self._cooperative_sleep(category, max(0.01, wait_for))

    def _publish_post_request_delays(
        self, category: RpcCategory, *, release_active: bool
    ) -> None:
        """Start fresh cooldowns after the completed Telegram request."""

        now = self._monotonic()
        api_ready_at = now + self._api_delay()
        category_delay = self._category_delay(category)
        with self._state_lock:
            self.__class__._next_start = max(
                self.__class__._next_start, api_ready_at
            )
            if category_delay > 0:
                self.__class__._category_next_start[category] = max(
                    self.__class__._category_next_start.get(category, 0.0),
                    now + category_delay,
                )
            if release_active:
                self.__class__._active = False

    @asynccontextmanager
    async def request_slot(
        self, category: RpcCategory | str = RpcCategory.READ
    ) -> AsyncIterator[None]:
        normalized = (
            category
            if isinstance(category, RpcCategory)
            else RpcCategory(str(category))
        )
        await self._wait_for_slot(normalized)
        try:
            yield
        finally:
            self._publish_post_request_delays(normalized, release_active=True)

    @asynccontextmanager
    async def reentrant_request_slot(
        self, category: RpcCategory | str = RpcCategory.READ
    ) -> AsyncIterator[None]:
        normalized = (
            category
            if isinstance(category, RpcCategory)
            else RpcCategory(str(category))
        )
        await self._wait_for_reentrant_slot(normalized)
        try:
            yield
        finally:
            self._publish_post_request_delays(normalized, release_active=False)

    async def acquire(self, category: RpcCategory | str = RpcCategory.READ) -> None:
        async with self.request_slot(category):
            return

    def category_wait_remaining(
        self, category: RpcCategory | str
    ) -> float:
        """Return the process-wide cooldown left for ``category``.

        This read-only helper lets resumable workflows persist the exact local
        JOIN wait before sleeping. The request gate remains authoritative if a
        different worker wins the next slot after the checkpointed wait.
        """

        normalized = (
            category
            if isinstance(category, RpcCategory)
            else RpcCategory(str(category))
        )
        now = self._monotonic()
        with self._state_lock:
            return max(
                0.0,
                self.__class__._category_next_start.get(normalized, 0.0) - now,
            )

    @classmethod
    def category_snapshot(cls) -> dict[str, int]:
        with cls._state_lock:
            return {
                category.value: int(cls._category_counts.get(category, 0))
                for category in RpcCategory
            }

    @classmethod
    def cooldown_snapshot(cls) -> dict[str, float]:
        with cls._state_lock:
            return {
                "global_next_start": float(cls._next_start),
                **{
                    category.value: float(cls._category_next_start.get(category, 0.0))
                    for category in RpcCategory
                },
            }

    @classmethod
    def _reset_for_tests(cls) -> None:
        with cls._state_lock:
            cls._active = False
            cls._next_start = 0.0
            cls._process_interval_floor = cls.MIN_INTERVAL_SECONDS
            cls._category_next_start = {category: 0.0 for category in RpcCategory}
            cls._category_counts = {category: 0 for category in RpcCategory}
