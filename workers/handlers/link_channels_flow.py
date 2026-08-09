"""Resumable link-discovery workflow with explicit checkpoint boundaries."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, cast

from core.account_restriction import get_account_restriction_state
from core.campaign_schedule import utc_now
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TaskPausedError,
    TelegramOperationError,
)
from workers.handlers.link_channel_decisions import (
    DeferredLinkDisposition,
    LinkErrorDisposition,
    deferred_link_disposition,
    group_link_status,
    link_error_disposition,
)
from workers.rpc_boundary import dispatch_barrier_kwargs

log = logging.getLogger(__name__)


class ChannelStepResult(StrEnum):
    COMPLETE = "complete"
    ADVANCED = "advanced"
    STOP_TASK = "stop_task"


@dataclass(slots=True)
class ChannelWork:
    channel_id: int
    row: dict[str, Any]
    number: int
    title: Any
    resolved_linked_id: int | None = None
    resolved_linked_title: str | None = None


@dataclass(slots=True)
class LinkChannelsRunner:
    owner: Any
    telegram: Any
    worker_db: Any
    linked: Any
    set_runtime: Any
    publish_activity: Any
    task: dict[str, Any]

    task_id: int = field(init=False)
    payload: dict[str, Any] = field(init=False)
    strict_repository: bool = field(init=False)
    account_id: int = field(init=False)
    checkpoint: dict[str, Any] = field(init=False, default_factory=dict)
    channel_by_id: dict[int, dict[str, Any]] = field(init=False, default_factory=dict)
    group_by_id: dict[int, dict[str, Any]] = field(init=False, default_factory=dict)
    known_group_ids: set[int] = field(init=False, default_factory=set)
    channel_ids: list[int] = field(init=False, default_factory=list)
    group_ids: list[int] = field(init=False, default_factory=list)
    channel_index: int = field(init=False, default=0)
    group_index: int = field(init=False, default=0)
    join_attempt_count: int = field(init=False, default=0)
    joined_count: int = field(init=False, default=0)
    prepared_count: int = field(init=False, default=0)
    banned_count: int = field(init=False, default=0)
    resolved_discussion_ids: set[int] = field(init=False, default_factory=set)
    minimum_join_interval: float = field(init=False, default=0.0)
    join_delay_min: float = field(init=False, default=0.0)
    join_delay_max: float = field(init=False, default=0.0)
    check_delay_min: float = field(init=False, default=0.0)
    check_delay_max: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.task_id = int(self.task["id"])
        self.payload = dict(self.task.get("payload") or {})
        selected_account_id = self.owner._as_int(
            self.worker_db.get_setting("telegram.account_id", 0), 0
        )
        task_account_id = self.owner._as_int(self.payload.get("account_id"), 0)
        self.strict_repository = type(self.worker_db).__module__.startswith("storage.")
        self.account_id = task_account_id or selected_account_id
        if self.strict_repository and (
            self.account_id <= 0 or selected_account_id != self.account_id
        ):
            raise NonRetryableTelegramError(
                "Задача связок принадлежит другому Telegram-аккаунту",
                code="account_state_mismatch",
                details={
                    "task_account_id": task_account_id,
                    "current_account_id": selected_account_id,
                },
            )
        self._configure_delays()

    def _configure_delays(self) -> None:
        config = self.owner.config
        self.minimum_join_interval = max(
            0.0, float(getattr(config, "min_join_interval_seconds", 45))
        )
        self.join_delay_min = max(
            0.0, float(getattr(config, "link_join_delay_min_seconds", 15))
        )
        self.join_delay_max = max(
            self.join_delay_min,
            float(getattr(config, "link_join_delay_max_seconds", 25)),
        )
        self.check_delay_min = max(
            0.0, float(getattr(config, "link_check_delay_min_seconds", 7))
        )
        self.check_delay_max = max(
            self.check_delay_min,
            float(getattr(config, "link_check_delay_max_seconds", 12)),
        )

    def require_account_binding(self) -> None:
        if not self.strict_repository:
            return
        current_account_id = self.owner._as_int(
            self.worker_db.get_setting("telegram.account_id", 0), 0
        )
        if current_account_id != self.account_id:
            raise NonRetryableTelegramError(
                "Telegram-аккаунт изменён во время подготовки связок",
                code="account_state_mismatch",
                details={
                    "task_account_id": self.account_id,
                    "current_account_id": current_account_id,
                },
            )

    def _load_rows(self) -> list[dict[str, Any]]:
        rows = list(
            self.worker_db.get_channels(account_id=self.account_id)
            if self.strict_repository
            else self.worker_db.get_channels()
        )
        register_peer = getattr(self.telegram, "register_peer_reference", None)
        if callable(register_peer):
            for row in rows:
                peer_id = row.get("channel_id")
                if peer_id is None:
                    continue
                register_peer(
                    peer_id,
                    access_hash=row.get("access_hash"),
                    peer_type=row.get("peer_type"),
                )
        return rows

    def _checkpoint_is_valid(self, checkpoint: object) -> bool:
        return bool(
            isinstance(checkpoint, dict)
            and int(checkpoint.get("version") or 0) == 1
            and self.owner._as_int(checkpoint.get("account_id"), 0)
            == self.account_id
            and isinstance(checkpoint.get("channel_ids"), list)
            and isinstance(checkpoint.get("group_ids"), list)
        )

    @staticmethod
    def _normalized_ids(value: object) -> list[int]:
        result: list[int] = []
        if not isinstance(value, list):
            return result
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError, OverflowError):
                continue
        return result

    def _build_new_checkpoint(self, task: dict[str, Any]) -> dict[str, Any]:
        legacy_completed = 0
        legacy_progress = max(0, min(100, self.owner._as_int(task.get("progress"), 0)))
        legacy_defer_count = max(0, self.owner._as_int(task.get("defer_count"), 0))
        snapshot_total = len(self.channel_by_id) + len(self.group_by_id)
        if legacy_defer_count > 0 and legacy_progress > 0 and snapshot_total > 0:
            exact_candidates = [
                index
                for index in range(snapshot_total + 1)
                if round(index * 100 / snapshot_total) == legacy_progress
            ]
            if exact_candidates:
                legacy_completed = min(exact_candidates)
            else:
                legacy_completed = max(
                    0,
                    min(
                        snapshot_total,
                        int(legacy_progress * snapshot_total / 100),
                    ),
                )

        channel_index = min(len(self.channel_by_id), legacy_completed)
        group_index = max(
            0,
            min(len(self.group_by_id), legacy_completed - channel_index),
        )
        completed_ids = list(self.channel_by_id)[:channel_index]
        prior_rows = [
            self.channel_by_id[channel_id]
            for channel_id in completed_ids
            if channel_id in self.channel_by_id
        ]
        checkpoint = {
            "version": 1,
            "account_id": self.account_id,
            "phase": "channels",
            "channel_ids": list(self.channel_by_id),
            "group_ids": list(self.group_by_id),
            "channel_index": channel_index,
            "group_index": group_index,
            "join_attempt_count": sum(
                1
                for row in prior_rows
                if "вступлен" in str(row.get("link_status") or "").lower()
            ),
            "joined_count": sum(
                1
                for row in prior_rows
                if str(row.get("link_status") or "")
                == "Связано · вступление выполнено"
            ),
            "prepared_count": sum(
                1
                for row in prior_rows
                if str(row.get("link_status") or "")
                in {
                    "Связано · вступление выполнено",
                    "Связано · участие подтверждено",
                }
            ),
            "banned_count": sum(
                1
                for row in prior_rows
                if row.get("local_banned_at")
                or str(row.get("link_status") or "").startswith("Заблокирован ·")
            ),
        }
        self.payload["_link_checkpoint"] = checkpoint
        initial_progress = (
            min(100, round(legacy_completed * 100 / snapshot_total))
            if snapshot_total > 0
            else 0
        )
        changed = self.worker_db.update_task_checkpoint(
            self.task_id, self.payload, initial_progress
        )
        if changed is False:
            raise RuntimeError("Could not initialize link task checkpoint")
        if legacy_completed:
            log.info(
                "Migrated legacy deferred link task %s to cursor %s/%s",
                self.task_id,
                legacy_completed,
                snapshot_total,
            )
        return checkpoint

    def initialize_checkpoint(self) -> None:
        all_rows = self._load_rows()
        self.known_group_ids = {
            int(row["channel_id"])
            for row in all_rows
            if row.get("channel_id") is not None
            and str(row.get("target_kind") or "channel") == "group"
            and not row.get("local_banned_at")
        }
        raw_checkpoint = self.payload.get("_link_checkpoint")
        checkpoint_valid = self._checkpoint_is_valid(raw_checkpoint)
        working_rows = (
            all_rows
            if checkpoint_valid
            else [
                row
                for row in all_rows
                if not row.get("link_checked_at") and not row.get("local_banned_at")
            ]
        )
        channels = [
            row
            for row in working_rows
            if str(row.get("target_kind") or "channel") == "channel"
        ]
        groups = [
            row
            for row in working_rows
            if str(row.get("target_kind") or "channel") == "group"
        ]
        self.channel_by_id = {
            int(row["channel_id"]): row
            for row in channels
            if row.get("channel_id") is not None
        }
        self.group_by_id = {
            int(row["channel_id"]): row
            for row in groups
            if row.get("channel_id") is not None
        }
        self.checkpoint = (
            cast(dict[str, Any], raw_checkpoint)
            if checkpoint_valid
            else self._build_new_checkpoint(self.task)
        )
        if not isinstance(self.checkpoint, dict):
            raise NonRetryableTelegramError(
                "Link task checkpoint is not a mapping",
                code="invalid_payload",
            )
        self._restore_checkpoint_state()

    def _restore_checkpoint_state(self) -> None:
        self.channel_ids = self._normalized_ids(self.checkpoint.get("channel_ids"))
        self.group_ids = self._normalized_ids(self.checkpoint.get("group_ids"))
        self.channel_index = max(
            0,
            min(
                len(self.channel_ids),
                self.owner._as_int(self.checkpoint.get("channel_index"), 0),
            ),
        )
        self.group_index = max(
            0,
            min(
                len(self.group_ids),
                self.owner._as_int(self.checkpoint.get("group_index"), 0),
            ),
        )
        self.join_attempt_count = max(
            0, self.owner._as_int(self.checkpoint.get("join_attempt_count"), 0)
        )
        self.joined_count = max(
            0, self.owner._as_int(self.checkpoint.get("joined_count"), 0)
        )
        self.prepared_count = max(
            0, self.owner._as_int(self.checkpoint.get("prepared_count"), 0)
        )
        self.banned_count = max(
            0, self.owner._as_int(self.checkpoint.get("banned_count"), 0)
        )
        self.resolved_discussion_ids = {
            int(self.channel_by_id[channel_id]["linked_chat_id"])
            for channel_id in self.channel_ids[: self.channel_index]
            if channel_id in self.channel_by_id
            and self.channel_by_id[channel_id].get("linked_chat_id") is not None
            and int(self.channel_by_id[channel_id]["linked_chat_id"])
            in self.group_by_id
        }

    def completed_count(self) -> int:
        return int(self.channel_index) + int(self.group_index)

    def progress_value(self) -> int:
        total = max(1, len(self.channel_ids) + len(self.group_ids))
        return min(100, round(self.completed_count() * 100 / total))

    def announce_start(self) -> None:
        total = len(self.channel_ids) + len(self.group_ids)
        if self.owner._as_int(self.task.get("defer_count"), 0) > 0 or self.completed_count() > 0:
            message = (
                "Связки продолжены с сохранённой позиции: "
                f"обработано {self.completed_count()} из {total}"
            )
        else:
            message = (
                f"Связки запущены: каналов {len(self.channel_ids)}, "
                f"групп {len(self.group_ids)}"
            )
        self.set_runtime(
            self.task_id,
            message,
            activity=True,
            account_id=self.account_id,
        )

    def persist_checkpoint(self, *, phase: str) -> None:
        self.checkpoint.update(
            {
                "phase": phase,
                "channel_index": self.channel_index,
                "group_index": self.group_index,
                "join_attempt_count": self.join_attempt_count,
                "joined_count": self.joined_count,
                "prepared_count": self.prepared_count,
                "banned_count": self.banned_count,
            }
        )
        self.payload["_link_checkpoint"] = self.checkpoint
        changed = self.worker_db.update_task_checkpoint(
            self.task_id, self.payload, self.progress_value()
        )
        if changed is False:
            raise RuntimeError("Could not persist link task checkpoint")

    def pause_requested(self) -> bool:
        checker = getattr(self.owner.queue_worker, "is_scope_cancelled", None)
        return bool(callable(checker) and checker("task", self.task_id))

    def pause_at_checkpoint(self, *, phase: str) -> None:
        self.persist_checkpoint(phase=phase)
        self.set_runtime(
            self.task_id,
            "Остановлено пользователем · позиция сохранена",
            activity=True,
        )
        raise TaskPausedError("Остановлено пользователем; прогресс связок сохранён")

    async def wait_between_checks(self, label: str, *, phase: str) -> None:
        if self.completed_count() <= 0 or self.check_delay_max <= 0:
            return
        delay = random.uniform(self.check_delay_min, self.check_delay_max)
        self.set_runtime(
            self.task_id,
            f"Пауза между проверками: {round(delay)} сек · {label}",
            activity=True,
        )
        if not await self.owner.queue_worker.safe_sleep(delay):
            raise asyncio.CancelledError
        if self.pause_requested():
            self.pause_at_checkpoint(phase=phase)

    def _load_current_channel(self, channel_id: int) -> dict[str, Any] | None:
        channel = self.channel_by_id.get(channel_id)
        if channel is None:
            return None
        getter = getattr(self.worker_db, "get_channel_by_id", None)
        if not callable(getter):
            return channel
        refreshed = getter(channel_id, account_id=self.account_id)
        if refreshed is None:
            return None
        if isinstance(refreshed, dict):
            self.channel_by_id[channel_id] = refreshed
            return refreshed
        return channel

    def _current_channel_allows_rpc(
        self, work: ChannelWork, related_peer_id: int | None = None
    ) -> bool:
        if self.pause_requested():
            return False
        if self.strict_repository:
            current_account_id = self.owner._as_int(
                self.worker_db.get_setting("telegram.account_id", 0), 0
            )
            if self.account_id <= 0 or current_account_id != self.account_id:
                return False
            if get_account_restriction_state(
                self.worker_db, account_id=self.account_id
            ).get("active"):
                return False
        getter = getattr(self.worker_db, "get_channel_by_id", None)
        if callable(getter):
            current = getter(work.channel_id, account_id=self.account_id)
            if current is None:
                return False
            if isinstance(current, dict) and bool(current.get("local_banned_at")):
                return False
        checker = getattr(type(self.worker_db), "is_channel_locally_banned", None)
        if callable(checker):
            if checker(self.worker_db, work.channel_id, account_id=self.account_id):
                return False
            if related_peer_id is not None and checker(
                self.worker_db, related_peer_id, account_id=self.account_id
            ):
                return False
        return True

    def _create_join_dispatch_barrier(self, work: ChannelWork, related_peer_id: int):
        factory = getattr(
            type(self.owner.queue_worker), "create_scope_dispatch_barrier", None
        )
        if not callable(factory):
            return None
        return factory(
            self.owner.queue_worker,
            ("task", self.task_id),
            ("channel", work.channel_id, self.account_id),
            ("channel", related_peer_id, self.account_id),
            pre_dispatch_check=lambda: self._current_channel_allows_rpc(
                work, related_peer_id
            ),
        )

    def _commit_channel_ban(self, work: ChannelWork, reason: str) -> bool:
        banner = getattr(type(self.worker_db), "ban_channel_locally", None)
        bound_banner = None
        if not callable(banner):
            bound_banner = getattr(self.worker_db, "ban_channel_locally", None)
            if not callable(bound_banner):
                return False

        def mutation():
            if callable(banner):
                changed = banner(
                    self.worker_db,
                    work.channel_id,
                    reason,
                    related_peer_id=work.resolved_linked_id,
                    account_id=self.account_id,
                )
            else:
                fallback_banner = cast(Any, bound_banner)
                changed = fallback_banner(
                    work.channel_id,
                    reason,
                    related_peer_id=work.resolved_linked_id,
                    account_id=self.account_id,
                )
            if changed is False:
                raise RuntimeError(
                    "Ambiguous Join target disappeared before local ban"
                )
            return bool(changed)

        runner = getattr(self.owner.queue_worker, "cancel_scopes_and_run", None)
        scopes = [("channel", work.channel_id, self.account_id)]
        if work.resolved_linked_id is not None:
            scopes.append(
                ("channel", int(work.resolved_linked_id), self.account_id)
            )
        if callable(runner):
            return bool(runner(scopes, mutation))
        return bool(mutation())

    def _update_channel_link(
        self,
        work: ChannelWork,
        linked_id: int | None,
        title: str | None,
        status: str,
    ) -> None:
        if self.strict_repository:
            self.worker_db.update_channel_link(
                work.channel_id,
                linked_id,
                title,
                status,
                account_id=self.account_id,
            )
        else:
            self.worker_db.update_channel_link(
                work.channel_id, linked_id, title, status
            )

    def _finalize_channel_link(
        self,
        work: ChannelWork,
        linked_id: int | None,
        title: str | None,
        status: str,
    ) -> None:
        """Persist a completed link result and checked marker as one durable step."""
        if self.strict_repository:
            changed = self.worker_db.finalize_channel_link_check(
                work.channel_id,
                linked_id,
                title,
                status,
                account_id=self.account_id,
            )
        else:
            self.worker_db.update_channel_link(
                work.channel_id, linked_id, title, status
            )
            changed = self.worker_db.mark_link_checked(
                work.channel_id, account_id=self.account_id
            )
        if changed is False:
            raise RuntimeError("Could not finalize channel link result")

    def _join_guard(self) -> dict[str, Any]:
        reader = getattr(self.worker_db, "get_join_guard", None)
        guard = (
            reader(
                max_joins=max(
                    1,
                    int(getattr(self.owner.config, "max_joins_per_hour", 40)),
                ),
                min_interval_seconds=self.minimum_join_interval,
                window_seconds=3600,
                account_id=self.account_id if self.account_id > 0 else None,
            )
            if callable(reader)
            else {"allowed": True, "wait_seconds": 0}
        )
        if not isinstance(guard, dict):
            return {"allowed": True, "wait_seconds": 0}
        return guard

    def _postpone_for_join_limit(self, guard: dict[str, Any]) -> None:
        wait = max(
            30,
            int(guard.get("wait_seconds") or 0) + random.randint(5, 20),
        )
        self.persist_checkpoint(phase="channels")
        self.set_runtime(
            self.task_id,
            "Локальный лимит вступлений: позиция сохранена; "
            f"автопродолжение через {max(1, round(wait / 60))} мин",
            activity=True,
            level="INFO",
            account_id=self.account_id,
        )
        postponement = getattr(
            self.worker_db, "postpone_running_task_for_account_cooldown", None
        )
        if not callable(postponement) or not postponement(
            self.task_id,
            retry_at=utc_now() + timedelta(seconds=wait),
            code=f"local_join_rate_wait: локальный лимит вступлений, повтор через {wait} сек",
        ):
            raise RuntimeError("Could not postpone link task for local join limit")

    async def _prepare_linked_channel(
        self, work: ChannelWork, linked_id: int
    ) -> ChannelStepResult:
        work.resolved_linked_id = linked_id
        if linked_id in self.known_group_ids:
            self.prepared_count += 1
            status = "Связано · обсуждение уже в диалогах"
        else:
            guard = self._join_guard()
            if not bool(guard.get("allowed", True)):
                self._postpone_for_join_limit(guard)
                return ChannelStepResult.STOP_TASK
            if (
                self.join_attempt_count > 0
                and int(guard.get("effective_count") or 0) <= 0
            ):
                delay = random.uniform(self.join_delay_min, self.join_delay_max)
                self.set_runtime(
                    self.task_id,
                    f"Пауза между вступлениями: {round(delay)} сек",
                    activity=True,
                )
                if not await self.owner.queue_worker.safe_sleep(delay):
                    raise asyncio.CancelledError
                if self.pause_requested():
                    self.pause_at_checkpoint(phase="channels")
            self.set_runtime(
                self.task_id,
                f"Подготовка обсуждения {work.number} из {len(self.channel_ids)}: {work.title}",
                activity=True,
            )
            if self.pause_requested():
                self.pause_at_checkpoint(phase="channels")
            if not self._current_channel_allows_rpc(work, linked_id):
                raise DeferredTelegramError(
                    "Local ban committed before Join dispatch",
                    code="local_ban_before_dispatch",
                    retry_after=1,
                )
            self.join_attempt_count += 1
            join_kwargs: dict[str, Any] = {}
            join_barrier = self._create_join_dispatch_barrier(work, linked_id)
            if join_barrier is not None:
                join_kwargs["dispatch_barrier"] = join_barrier
            newly_joined = bool(
                await self.telegram.join_without_confirmation(
                    linked_id, **join_kwargs
                )
            )
            self.prepared_count += 1
            if newly_joined:
                self.joined_count += 1
                self.worker_db.record_join_event(
                    linked_id,
                    "joined",
                    account_id=self.account_id if self.account_id > 0 else None,
                )
                status = "Связано · вступление выполнено"
            else:
                status = "Связано · участие уже было"
        self._finalize_channel_link(work, linked_id, None, status)
        if linked_id in self.group_by_id:
            self.resolved_discussion_ids.add(linked_id)
        self.set_runtime(
            self.task_id,
            f"Канал {work.number} из {len(self.channel_ids)}: {work.title} · {status}",
            activity=True,
        )
        return ChannelStepResult.COMPLETE

    def _advance_channel(self, *, mark_checked: bool) -> None:
        channel_id = self.channel_ids[self.channel_index]
        if mark_checked:
            self.worker_db.mark_link_checked(
                channel_id, account_id=self.account_id
            )
        self.channel_index += 1
        self.persist_checkpoint(phase="channels")

    def _handle_deferred_channel(
        self, work: ChannelWork, exc: DeferredTelegramError
    ) -> ChannelStepResult:
        disposition = deferred_link_disposition(getattr(exc, "code", ""))
        if disposition is DeferredLinkDisposition.PAUSE:
            self.pause_at_checkpoint(phase="channels")
        if disposition is DeferredLinkDisposition.LOCAL_BAN:
            if self.strict_repository and get_account_restriction_state(
                self.worker_db, account_id=self.account_id
            ).get("active"):
                raise NonRetryableTelegramError(
                    "Telegram account is restricted before RPC dispatch",
                    code="user_restricted",
                ) from exc
            current_account_id = (
                self.owner._as_int(
                    self.worker_db.get_setting("telegram.account_id", 0), 0
                )
                if self.strict_repository
                else self.account_id
            )
            if self.strict_repository and current_account_id != self.account_id:
                raise NonRetryableTelegramError(
                    "Telegram account changed before RPC dispatch",
                    code="account_state_mismatch",
                ) from exc
            self._commit_channel_ban(
                work, "Цель была локально заблокирована до отправки Join"
            )
            self.banned_count += 1
            self._advance_channel(mark_checked=False)
            self.set_runtime(
                self.task_id,
                f"Канал {work.number} из {len(self.channel_ids)}: {work.title} · "
                "локально заблокирован до Join; очередь продолжена",
                activity=True,
                level="WARNING",
            )
            return ChannelStepResult.ADVANCED

        self._finalize_channel_link(
            work,
            None,
            None,
            "Пропущено · Telegram FloodWait",
        )
        self._advance_channel(mark_checked=False)
        self.set_runtime(
            self.task_id,
            f"Канал {work.number} из {len(self.channel_ids)}: {work.title} · "
            "пропущен из-за FloodWait; повторной проверки не будет",
            activity=True,
            level="WARNING",
        )
        raise exc

    def _handle_nonretryable_channel(
        self, work: ChannelWork, exc: NonRetryableTelegramError
    ) -> ChannelStepResult:
        disposition = link_error_disposition(getattr(exc, "code", ""))
        if disposition is LinkErrorDisposition.UNKNOWN_BAN:
            if not self._commit_channel_ban(
                work, "Результат вступления неизвестен"
            ):
                self._update_channel_link(
                    work,
                    None,
                    None,
                    "Заблокирован · результат вступления неизвестен",
                )
                self.worker_db.mark_link_checked(
                    work.channel_id, account_id=self.account_id
                )
            self.banned_count += 1
            self._advance_channel(mark_checked=False)
            log.warning(
                "Channel locally banned after ambiguous Join: account_id=%s "
                "channel_id=%s related_peer_id=%s",
                self.account_id,
                work.channel_id,
                work.resolved_linked_id,
            )
            self.set_runtime(
                self.task_id,
                f"Канал {work.number} из {len(self.channel_ids)}: {work.title} · "
                "заблокирован; очередь продолжена",
                activity=True,
                level="WARNING",
            )
            return ChannelStepResult.ADVANCED

        status = (
            "Связано · заявка на вступление отправлена"
            if disposition is LinkErrorDisposition.JOIN_REQUESTED
            else f"Недоступно: {exc}"
        )
        if disposition is LinkErrorDisposition.RAISE_RESTRICTION:
            self._update_channel_link(
                work,
                work.resolved_linked_id,
                work.resolved_linked_title,
                status,
            )
        else:
            self._finalize_channel_link(
                work,
                work.resolved_linked_id,
                work.resolved_linked_title,
                status,
            )
        self.set_runtime(
            self.task_id,
            f"Канал {work.number} из {len(self.channel_ids)}: {work.title} · {status}",
            activity=True,
            level="WARNING",
        )
        if disposition is LinkErrorDisposition.RAISE_RESTRICTION:
            raise exc
        return ChannelStepResult.COMPLETE

    async def process_channel(self, work: ChannelWork) -> ChannelStepResult:
        try:
            if not self._current_channel_allows_rpc(work):
                if self.pause_requested():
                    self.pause_at_checkpoint(phase="channels")
                self.banned_count += 1
                self._advance_channel(mark_checked=False)
                return ChannelStepResult.ADVANCED
            resolver = self.linked.get_linked_chat_id
            barrier = self._create_join_dispatch_barrier(work, work.channel_id)
            linked_id = await resolver(
                work.channel_id,
                **dispatch_barrier_kwargs(resolver, barrier),
            )
            if linked_id is None:
                self._finalize_channel_link(
                    work, None, None, "Нет чата обсуждения"
                )
                self.set_runtime(
                    self.task_id,
                    f"Канал {work.number} из {len(self.channel_ids)}: {work.title} · "
                    "нет чата обсуждения",
                    activity=True,
                )
                return ChannelStepResult.COMPLETE
            return await self._prepare_linked_channel(work, int(linked_id))
        except asyncio.CancelledError:
            raise
        except DeferredTelegramError as exc:
            return self._handle_deferred_channel(work, exc)
        except NonRetryableTelegramError as exc:
            return self._handle_nonretryable_channel(work, exc)
        except TelegramOperationError:
            raise
        except Exception:
            log.exception("Could not link or prepare channel %s", work.channel_id)
            raise

    async def process_channels(self) -> bool:
        while self.channel_index < len(self.channel_ids):
            self.require_account_binding()
            if self.owner.queue_worker.isInterruptionRequested():
                raise asyncio.CancelledError
            if self.pause_requested():
                self.pause_at_checkpoint(phase="channels")
            channel_id = self.channel_ids[self.channel_index]
            channel = self._load_current_channel(channel_id)
            if channel is None:
                self.publish_activity(
                    f"Канал {self.channel_index + 1} пропущен: удалён из списка во время ожидания"
                )
                self._advance_channel(mark_checked=False)
                continue
            if channel.get("local_banned_at"):
                number = self.channel_index + 1
                title = channel.get("title") or channel_id
                self.banned_count += 1
                self._advance_channel(mark_checked=False)
                self.set_runtime(
                    self.task_id,
                    f"Канал {number} из {len(self.channel_ids)}: {title} · уже "
                    "локально заблокирован; Telegram-запросы пропущены, очередь продолжена",
                    activity=True,
                    level="WARNING",
                )
                continue
            if channel.get("link_checked_at"):
                # A completed durable result may be ahead of a stale checkpoint
                # after a process crash. Never replay Telegram RPCs for it.
                self._advance_channel(mark_checked=False)
                continue

            number = self.channel_index + 1
            title = channel.get("title") or channel_id
            await self.wait_between_checks(
                f"следующий канал {number} из {len(self.channel_ids)}",
                phase="channels",
            )
            self.set_runtime(
                self.task_id,
                f"Связка {number} из {len(self.channel_ids)}: {title}",
                activity=True,
            )
            result = await self.process_channel(
                ChannelWork(channel_id, channel, number, title, channel.get("linked_chat_id"))
            )
            if result is ChannelStepResult.STOP_TASK:
                return False
            if result is ChannelStepResult.ADVANCED:
                continue
            self._advance_channel(mark_checked=False)
            if self.pause_requested():
                raise TaskPausedError(
                    "Остановлено пользователем; прогресс связок сохранён"
                )
        return True

    def process_groups(self) -> None:
        if str(self.checkpoint.get("phase") or "") != "groups":
            self.persist_checkpoint(phase="groups")
        while self.group_index < len(self.group_ids):
            self.require_account_binding()
            if self.owner.queue_worker.isInterruptionRequested():
                raise asyncio.CancelledError
            if self.pause_requested():
                self.pause_at_checkpoint(phase="groups")
            group_id = self.group_ids[self.group_index]
            group = self.group_by_id.get(group_id)
            if group is None:
                self.publish_activity(
                    f"Группа {self.group_index + 1} пропущена: удалена из списка во время ожидания"
                )
                self.group_index += 1
                self.persist_checkpoint(phase="groups")
                continue
            group_number = self.group_index + 1
            is_linked = (
                str(group.get("comment_mode") or "") == "linked_discussion"
                or group_id in self.resolved_discussion_ids
            )
            kwargs: dict[str, Any] = {
                "is_linked": is_linked,
                "status": group_link_status(is_linked),
            }
            if self.account_id > 0:
                kwargs["account_id"] = self.account_id
            self.worker_db.update_group_link_classification(group_id, **kwargs)
            self.worker_db.mark_link_checked(
                group_id, account_id=self.account_id
            )
            self.group_index += 1
            self.persist_checkpoint(phase="groups")
            if group_number == len(self.group_ids) or group_number % 50 == 0:
                self.set_runtime(
                    self.task_id,
                    f"Локально классифицировано групп: {group_number} из {len(self.group_ids)}",
                    activity=True,
                )
            if self.pause_requested():
                raise TaskPausedError(
                    "Остановлено пользователем; прогресс связок сохранён"
                )

    def finalize(self) -> None:
        self.require_account_binding()
        if self.strict_repository:
            self.worker_db.refresh_group_comment_modes(account_id=self.account_id)
        else:
            self.worker_db.refresh_group_comment_modes()
        self.set_runtime(
            self.task_id,
            "Связки подготовлены: "
            f"каналов {len(self.channel_ids)}, участие подтверждено {self.prepared_count}, "
            f"новых вступлений {self.joined_count}, локально заблокировано {self.banned_count}, "
            "обычные группы готовы к сообщениям без привязки к посту",
            activity=True,
        )
        self.payload.pop("_link_checkpoint", None)
        changed = self.worker_db.update_task_checkpoint(
            self.task_id, self.payload, 100
        )
        if changed is False:
            raise RuntimeError("Could not finalize link task checkpoint")
        self.worker_db.update_task_progress(self.task_id, 100)

    async def run(self) -> None:
        self.initialize_checkpoint()
        self.announce_start()
        if not await self.process_channels():
            return
        self.process_groups()
        self.finalize()
