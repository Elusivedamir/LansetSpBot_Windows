"""Hardened resumable link workflow: revalidation, checkpointed waits and honest labels."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import Any, cast

from core.campaign_schedule import from_db_time, utc_now
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from workers.handlers.link_channel_decisions import (
    DeferredLinkDisposition,
    deferred_link_disposition,
)
from workers.handlers.link_channels_flow import (
    ChannelStepResult,
    ChannelWork,
    LinkChannelsRunner,
)


class HardenedLinkChannelsRunner(LinkChannelsRunner):
    """Preserve the existing cursor architecture while fixing pacing/cache defects."""

    REVALIDATE_AFTER_SECONDS = 24 * 60 * 60

    def _configure_delays(self) -> None:
        # These are policy values, not a cosmetic status message.
        self.minimum_join_interval = 120.0
        self.join_delay_min = 120.0
        self.join_delay_max = 300.0
        self.check_delay_min = 12.0
        self.check_delay_max = 20.0

    @staticmethod
    def _needs_revalidation(row: dict[str, Any]) -> bool:
        if not row.get("link_checked_at"):
            return True
        status = str(row.get("link_status") or "").casefold()
        if any(
            marker in status
            for marker in (
                "не провер",
                "недоступ",
                "ошиб",
                "нет чата обсуждения",
                "заявка на вступление",
            )
        ):
            return True
        checked = from_db_time(row.get("link_checked_at"))
        if checked is None:
            return True
        synced = from_db_time(row.get("last_sync_at"))
        if synced is not None and synced > checked:
            return True
        return (
            utc_now() - checked
        ).total_seconds() >= HardenedLinkChannelsRunner.REVALIDATE_AFTER_SECONDS

    def initialize_checkpoint(self) -> None:
        all_rows = self._load_rows()
        raw_checkpoint = self.payload.get("_link_checkpoint")
        checkpoint_valid = self._checkpoint_is_valid(raw_checkpoint)
        force = bool(self.payload.get("force_recheck"))
        revalidate_cached = bool(self.payload.get("revalidate_cached", True))

        eligible_rows = [row for row in all_rows if not row.get("local_banned_at")]
        if checkpoint_valid:
            # The persisted cursor is authoritative. Keep the complete lookup map
            # so channels remaining in the checkpoint can be refreshed in place.
            working_rows = all_rows
        elif force:
            working_rows = eligible_rows
        else:
            working_rows = [row for row in eligible_rows if self._needs_revalidation(row)]
            if not working_rows and eligible_rows:
                # A fresh cache may reduce the scope, but it must never turn an
                # explicit user action into a fake 0% completion. Revalidate one
                # deterministic health-check target and report that exact scope.
                working_rows = [
                    min(
                        eligible_rows,
                        key=lambda row: (
                            from_db_time(row.get("link_checked_at")) or utc_now(),
                            int(row.get("channel_id") or 0),
                        ),
                    )
                ]
                self.payload["cache_health_check"] = True

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
                "Link task checkpoint is not a mapping", code="invalid_payload"
            )
        self.checkpoint["force_recheck"] = force
        self.checkpoint["revalidate_cached"] = revalidate_cached
        self._restore_checkpoint_state()

    def announce_start(self) -> None:
        mode = (
            "Принудительная перепроверка всех связок"
            if bool(self.payload.get("force_recheck"))
            else "Проверка новых, изменившихся и устаревших связок"
        )
        total = len(self.channel_ids) + len(self.group_ids)
        if self.completed_count() > 0:
            message = (
                f"{mode} продолжена с сохранённой позиции: обработано "
                f"{self.completed_count()} из {total}"
            )
        else:
            message = f"{mode} запущена: объектов {total}"
        self.set_runtime(
            self.task_id, message, activity=True, account_id=self.account_id
        )
        self.set_runtime(
            self.task_id,
            "Telegram API-паузы активны: 2–5 сек между отдельными RPC",
            activity=True,
            account_id=self.account_id,
        )

    async def _checkpointed_sleep(
        self,
        delay: float,
        *,
        wait_type: str,
        message: str,
        phase: str,
    ) -> None:
        seconds = max(0.0, float(delay))
        if seconds <= 0:
            return
        resume_at = utc_now() + timedelta(seconds=seconds)
        self.checkpoint.update(
            {
                "wait_type": wait_type,
                "wait_seconds": round(seconds),
                "resume_at": resume_at.isoformat(),
                "current_channel_index": self.channel_index,
                "current_group_index": self.group_index,
            }
        )
        self.persist_checkpoint(phase=phase)
        self.set_runtime(
            self.task_id,
            message,
            activity=True,
            account_id=self.account_id,
        )
        if not await self.owner.queue_worker.safe_sleep(seconds):
            raise asyncio.CancelledError
        self.checkpoint.pop("wait_type", None)
        self.checkpoint.pop("wait_seconds", None)
        self.checkpoint.pop("resume_at", None)
        self.persist_checkpoint(phase=phase)
        if self.pause_requested():
            self.pause_at_checkpoint(phase=phase)

    async def wait_between_checks(self, label: str, *, phase: str) -> None:
        del label
        if self.completed_count() <= 0:
            return
        delay = random.uniform(self.check_delay_min, self.check_delay_max)
        await self._checkpointed_sleep(
            delay,
            wait_type="channel_cooldown",
            message=f"Пауза между каналами: {round(delay)} сек",
            phase=phase,
        )

    @staticmethod
    def _duration(seconds: float) -> str:
        value = max(0, round(seconds))
        minutes, secs = divmod(value, 60)
        return f"{minutes:02d}:{secs:02d}"

    async def _resume_checkpoint_wait(self) -> None:
        resume_at = from_db_time(self.checkpoint.get("resume_at"))
        wait_type = str(self.checkpoint.get("wait_type") or "")
        if resume_at is None or not wait_type:
            return
        remaining = max(0.0, (resume_at - utc_now()).total_seconds())
        if remaining <= 0:
            self.checkpoint.pop("wait_type", None)
            self.checkpoint.pop("wait_seconds", None)
            self.checkpoint.pop("resume_at", None)
            self.persist_checkpoint(
                phase=str(self.checkpoint.get("phase") or "channels")
            )
            return
        label = {
            "local_join_cooldown": "Локальная пауза между вступлениями",
            "channel_cooldown": "Пауза между каналами",
            "telegram_flood_wait": "Telegram FloodWait",
        }.get(wait_type, "Сохранённая пауза")
        await self._checkpointed_sleep(
            remaining,
            wait_type=wait_type,
            message=f"{label}: продолжение через {self._duration(remaining)}",
            phase=str(self.checkpoint.get("phase") or "channels"),
        )

    async def run(self) -> None:
        self.initialize_checkpoint()
        self.announce_start()
        await self._resume_checkpoint_wait()
        if not await self.process_channels():
            return
        self.process_groups()
        self.finalize()

    async def _prepare_linked_channel(
        self, work: ChannelWork, linked_id: int
    ) -> ChannelStepResult:
        work.resolved_linked_id = linked_id
        if linked_id in self.group_by_id:
            self.prepared_count += 1
            status = "Связано · обсуждение уже в диалогах"
        else:
            guard = self._join_guard()
            if not bool(guard.get("allowed", True)):
                self._postpone_for_join_limit(guard)
                return ChannelStepResult.STOP_TASK

            limiter = getattr(self.telegram, "limiter", None)
            remaining_reader = getattr(limiter, "category_wait_remaining", None)
            if callable(remaining_reader):
                try:
                    delay = max(0.0, float(remaining_reader("JOIN")))
                except (TypeError, ValueError, OverflowError):
                    delay = 0.0
            elif self.join_attempt_count > 0:
                # Lightweight test doubles and legacy adapters do not expose the
                # shared limiter. Keep the same policy as a safe compatibility
                # fallback, while production uses the process-wide reservation.
                delay = random.uniform(self.join_delay_min, self.join_delay_max)
            else:
                delay = 0.0
            if delay > 0:
                await self._checkpointed_sleep(
                    delay,
                    wait_type="local_join_cooldown",
                    message=(
                        "Локальная пауза между вступлениями: "
                        f"{self._duration(delay)}"
                    ),
                    phase="channels",
                )

            self.set_runtime(
                self.task_id,
                f"Подготовка обсуждения {work.number} из {len(self.channel_ids)}: {work.title}",
                activity=True,
                account_id=self.account_id,
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
                await self.telegram.join_without_confirmation(linked_id, **join_kwargs)
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

        self._update_channel_link(work, linked_id, None, status)
        if linked_id in self.group_by_id:
            self.resolved_discussion_ids.add(linked_id)
        self.set_runtime(
            self.task_id,
            f"Канал {work.number} из {len(self.channel_ids)}: {work.title} · {status}",
            activity=True,
            account_id=self.account_id,
        )
        return ChannelStepResult.COMPLETE

    def _handle_deferred_channel(
        self, work: ChannelWork, exc: DeferredTelegramError
    ) -> ChannelStepResult:
        code = str(getattr(exc, "code", ""))
        disposition = deferred_link_disposition(code)
        if disposition is DeferredLinkDisposition.PAUSE:
            self.pause_at_checkpoint(phase="channels")
        if disposition is DeferredLinkDisposition.LOCAL_BAN:
            return super()._handle_deferred_channel(work, exc)

        if "flood_wait" in code or "floodwait" in code:
            total_wait = max(1, int(getattr(exc, "retry_after", 1)))
            cause = getattr(exc, "__cause__", None)
            server_wait = max(0, int(getattr(cause, "seconds", 0) or 0))
            safety_buffer = total_wait - server_wait if server_wait > 0 else 0
            self.checkpoint.update(
                {
                    "wait_type": "telegram_flood_wait",
                    "wait_seconds": total_wait,
                    "telegram_wait_seconds": server_wait or None,
                    "safety_buffer_seconds": safety_buffer or None,
                    "resume_at": (
                        utc_now() + timedelta(seconds=total_wait)
                    ).isoformat(),
                    "current_channel_index": self.channel_index,
                }
            )
            self.persist_checkpoint(phase="channels")
            wait_text = (
                f"Telegram FloodWait: {server_wait} сек + защитный запас "
                f"{safety_buffer} сек"
                if server_wait > 0 and 30 <= safety_buffer <= 45
                else f"Telegram FloodWait: защищённое ожидание {total_wait} сек"
            )
            self.set_runtime(
                self.task_id,
                wait_text + "; позиция сохранена",
                activity=True,
                level="WARNING",
                account_id=self.account_id,
            )
            # Crucially, do not advance or mark the current channel checked.
            raise exc

        return super()._handle_deferred_channel(work, exc)
