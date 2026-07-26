"""Link-discovery handler extracted from the worker handler registry.

``link_channels`` was an 825-line closure nested inside
``create_worker_handlers``, which made it impossible to exercise on its own.
It now follows the same factory shape as the comment-slot and join-slot
handlers: the six values it used to capture from the enclosing scope are passed
in explicitly, and the behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, cast

from core.account_restriction import get_account_restriction_state
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TaskPausedError,
    TelegramOperationError,
)
from workers.rpc_boundary import dispatch_barrier_kwargs

log = logging.getLogger(__name__)


def create_link_channels_handler(
    *,
    self,
    telegram: Any,
    worker_db: Any,
    linked: Any,
    set_runtime: Any,
    publish_activity: Any,
):
    """Return the ``link_channels`` queue handler bound to its collaborators."""

    async def link_channels(task: dict[str, Any]) -> None:
        """Resolve links once and resume from the last completed target.

        Telegram may defer a GetFullChannel/membership request for many minutes.
        The checkpoint is stored inside the task payload after every completed
        channel or group. A target that itself triggers FloodWait is permanently
        marked as skipped, so the resumed task starts from the following target.
        """

        task_id = int(task["id"])
        payload = dict(task.get("payload") or {})
        selected_account_id = self._as_int(
            worker_db.get_setting("telegram.account_id", 0), 0
        )
        task_account_id = self._as_int(payload.get("account_id"), 0)
        strict_repository = type(worker_db).__module__.startswith("storage.")
        account_id = task_account_id or selected_account_id
        if strict_repository and (
            account_id <= 0 or selected_account_id != account_id
        ):
            raise NonRetryableTelegramError(
                "Задача связок принадлежит другому Telegram-аккаунту",
                code="account_state_mismatch",
                details={
                    "task_account_id": task_account_id,
                    "current_account_id": selected_account_id,
                },
            )

        def require_account_binding() -> None:
            if not strict_repository:
                return
            current_account_id = self._as_int(
                worker_db.get_setting("telegram.account_id", 0), 0
            )
            if current_account_id != account_id:
                raise NonRetryableTelegramError(
                    "Telegram-аккаунт изменён во время подготовки связок",
                    code="account_state_mismatch",
                    details={
                        "task_account_id": account_id,
                        "current_account_id": current_account_id,
                    },
                )

        all_rows = list(
            worker_db.get_channels(account_id=account_id)
            if strict_repository
            else worker_db.get_channels()
        )
        register_peer = getattr(telegram, "register_peer_reference", None)
        if callable(register_peer):
            for persisted_row in all_rows:
                peer_id = persisted_row.get("channel_id")
                if peer_id is None:
                    continue
                register_peer(
                    peer_id,
                    access_hash=persisted_row.get("access_hash"),
                    peer_type=persisted_row.get("peer_type"),
                )
        checkpoint = payload.get("_link_checkpoint")
        checkpoint_valid = (
            isinstance(checkpoint, dict)
            and int(checkpoint.get("version") or 0) == 1
            and self._as_int(checkpoint.get("account_id"), 0) == account_id
            and isinstance(checkpoint.get("channel_ids"), list)
            and isinstance(checkpoint.get("group_ids"), list)
        )
        # A brand-new pass contains only targets that have never completed a link
        # inspection. A resumed task keeps its immutable snapshot and cursor.
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
        channel_by_id = {
            int(row["channel_id"]): row
            for row in channels
            if row.get("channel_id") is not None
        }
        group_by_id = {
            int(row["channel_id"]): row
            for row in groups
            if row.get("channel_id") is not None
        }

        join_delay_min = float(getattr(self.config, "link_join_delay_min_seconds", 15))
        join_delay_max = float(getattr(self.config, "link_join_delay_max_seconds", 25))
        if join_delay_max < join_delay_min:
            join_delay_max = join_delay_min

        check_delay_min = float(getattr(self.config, "link_check_delay_min_seconds", 3))
        check_delay_max = float(getattr(self.config, "link_check_delay_max_seconds", 7))
        check_delay_min = max(0.0, check_delay_min)
        if check_delay_max < check_delay_min:
            check_delay_max = check_delay_min

        if not checkpoint_valid:
            legacy_completed = 0
            legacy_progress = max(0, min(100, self._as_int(task.get("progress"), 0)))
            legacy_defer_count = max(0, self._as_int(task.get("defer_count"), 0))
            snapshot_total = len(channel_by_id) + len(group_by_id)
            if legacy_defer_count > 0 and legacy_progress > 0 and snapshot_total > 0:
                exact_candidates = [
                    index
                    for index in range(snapshot_total + 1)
                    if round(index * 100 / snapshot_total) == legacy_progress
                ]
                if exact_candidates:
                    # Choose the earliest exact cursor.  This may repeat at most a
                    # very small rounded-progress bucket, but never skips a target.
                    legacy_completed = min(exact_candidates)
                else:
                    legacy_completed = max(
                        0,
                        min(
                            snapshot_total,
                            int(legacy_progress * snapshot_total / 100),
                        ),
                    )

            legacy_channel_index = min(len(channel_by_id), legacy_completed)
            legacy_group_index = max(
                0,
                min(len(group_by_id), legacy_completed - legacy_channel_index),
            )
            previously_completed_channels = list(channel_by_id)[:legacy_channel_index]
            prior_rows = [
                channel_by_id[channel_id]
                for channel_id in previously_completed_channels
                if channel_id in channel_by_id
            ]
            legacy_joined_count = sum(
                1
                for row in prior_rows
                if str(row.get("link_status") or "") == "Связано · вступление выполнено"
            )
            legacy_prepared_count = sum(
                1
                for row in prior_rows
                if str(row.get("link_status") or "")
                in {
                    "Связано · вступление выполнено",
                    "Связано · участие подтверждено",
                }
            )
            legacy_join_attempt_count = sum(
                1
                for row in prior_rows
                if "вступлен" in str(row.get("link_status") or "").lower()
            )
            legacy_banned_count = sum(
                1
                for row in prior_rows
                if row.get("local_banned_at")
                or str(row.get("link_status") or "").startswith("Заблокирован ·")
            )
            checkpoint = {
                "version": 1,
                "account_id": account_id,
                "phase": "channels",
                "channel_ids": list(channel_by_id),
                "group_ids": list(group_by_id),
                "channel_index": legacy_channel_index,
                "group_index": legacy_group_index,
                "join_attempt_count": legacy_join_attempt_count,
                "joined_count": legacy_joined_count,
                "prepared_count": legacy_prepared_count,
                "banned_count": legacy_banned_count,
            }
            payload["_link_checkpoint"] = checkpoint
            initial_progress = (
                min(100, round(legacy_completed * 100 / snapshot_total))
                if snapshot_total > 0
                else 0
            )
            initial = worker_db.update_task_checkpoint(
                task_id, payload, initial_progress
            )
            if initial is False:
                raise RuntimeError("Could not initialize link task checkpoint")
            if legacy_completed:
                log.info(
                    "Migrated legacy deferred link task %s to cursor %s/%s",
                    task_id,
                    legacy_completed,
                    snapshot_total,
                )

        if not isinstance(checkpoint, dict):
            raise NonRetryableTelegramError(
                "Link task checkpoint is not a mapping",
                code="invalid_payload",
            )

        def normalized_ids(value: object) -> list[int]:
            result: list[int] = []
            if not isinstance(value, list):
                return result
            for item in value:
                try:
                    result.append(int(item))
                except (TypeError, ValueError, OverflowError):
                    continue
            return result

        channel_ids = normalized_ids(checkpoint.get("channel_ids"))
        group_ids = normalized_ids(checkpoint.get("group_ids"))
        channel_index = max(
            0,
            min(
                len(channel_ids),
                self._as_int(checkpoint.get("channel_index"), 0),
            ),
        )
        group_index = max(
            0,
            min(len(group_ids), self._as_int(checkpoint.get("group_index"), 0)),
        )
        join_attempt_count = max(
            0, self._as_int(checkpoint.get("join_attempt_count"), 0)
        )
        joined_count = max(0, self._as_int(checkpoint.get("joined_count"), 0))
        prepared_count = max(0, self._as_int(checkpoint.get("prepared_count"), 0))
        banned_count = max(0, self._as_int(checkpoint.get("banned_count"), 0))
        resolved_discussion_ids = {
            int(channel_by_id[channel_id]["linked_chat_id"])
            for channel_id in channel_ids[:channel_index]
            if channel_id in channel_by_id
            and channel_by_id[channel_id].get("linked_chat_id") is not None
            and int(channel_by_id[channel_id]["linked_chat_id"]) in group_by_id
        }
        total = max(1, len(channel_ids) + len(group_ids))

        def completed_count() -> int:
            return int(channel_index) + int(group_index)

        def progress_value() -> int:
            return min(100, round(completed_count() * 100 / total))

        if self._as_int(task.get("defer_count"), 0) > 0 or completed_count() > 0:
            set_runtime(
                task_id,
                "Связки продолжены с сохранённой позиции: "
                f"обработано {completed_count()} из {len(channel_ids) + len(group_ids)}",
                activity=True,
                account_id=account_id,
            )
        else:
            set_runtime(
                task_id,
                f"Связки запущены: каналов {len(channel_ids)}, групп {len(group_ids)}",
                activity=True,
                account_id=account_id,
            )

        def persist_checkpoint(*, phase: str) -> None:
            checkpoint.update(
                {
                    "phase": phase,
                    "channel_index": channel_index,
                    "group_index": group_index,
                    "join_attempt_count": join_attempt_count,
                    "joined_count": joined_count,
                    "prepared_count": prepared_count,
                    "banned_count": banned_count,
                }
            )
            payload["_link_checkpoint"] = checkpoint
            changed = worker_db.update_task_checkpoint(
                task_id, payload, progress_value()
            )
            if changed is False:
                raise RuntimeError("Could not persist link task checkpoint")

        def pause_requested() -> bool:
            checker = getattr(self.queue_worker, "is_scope_cancelled", None)
            return bool(callable(checker) and checker("task", task_id))

        def pause_at_checkpoint(*, phase: str) -> None:
            persist_checkpoint(phase=phase)
            set_runtime(
                task_id,
                "Остановлено пользователем · позиция сохранена",
                activity=True,
            )
            raise TaskPausedError("Остановлено пользователем; прогресс связок сохранён")

        async def wait_between_checks(label: str, *, phase: str) -> None:
            if completed_count() <= 0 or check_delay_max <= 0:
                return
            delay = random.uniform(check_delay_min, check_delay_max)
            set_runtime(
                task_id,
                f"Пауза между проверками: {round(delay)} сек · {label}",
                activity=True,
            )
            if not await self.queue_worker.safe_sleep(delay):
                raise asyncio.CancelledError
            if pause_requested():
                pause_at_checkpoint(phase=phase)

        while channel_index < len(channel_ids):
            require_account_binding()
            if self.queue_worker.isInterruptionRequested():
                raise asyncio.CancelledError
            if pause_requested():
                pause_at_checkpoint(phase="channels")
            channel_id = channel_ids[channel_index]
            channel = channel_by_id.get(channel_id)
            if channel is None:
                # The user may delete a row while a deferred task is waiting.
                publish_activity(
                    f"Канал {channel_index + 1} пропущен: удалён из списка во время ожидания"
                )
                channel_index += 1
                persist_checkpoint(phase="channels")
                continue

            # A checkpoint is an immutable work snapshot, not permission to bypass
            # current safety state. Re-read the row immediately before any delay or
            # Telegram RPC so a ban committed before a crash (or during a deferred
            # task) is enforced fail-closed after resume.
            get_channel_by_id = getattr(worker_db, "get_channel_by_id", None)
            if callable(get_channel_by_id):
                refreshed_channel = get_channel_by_id(channel_id, account_id=account_id)
                if refreshed_channel is None:
                    publish_activity(
                        f"Канал {channel_index + 1} пропущен: удалён из списка "
                        "во время ожидания"
                    )
                    channel_index += 1
                    persist_checkpoint(phase="channels")
                    continue
                if isinstance(refreshed_channel, dict):
                    channel = refreshed_channel
                    channel_by_id[channel_id] = refreshed_channel

            if channel.get("local_banned_at"):
                channel_number = channel_index + 1
                title_for_status = channel.get("title") or channel_id
                banned_count += 1
                channel_index += 1
                persist_checkpoint(phase="channels")
                set_runtime(
                    task_id,
                    f"Канал {channel_number} из {len(channel_ids)}: "
                    f"{title_for_status} · уже локально заблокирован; "
                    "Telegram-запросы пропущены, очередь продолжена",
                    activity=True,
                    level="WARNING",
                )
                continue

            channel_number = channel_index + 1
            title_for_status = channel.get("title") or channel_id
            await wait_between_checks(
                f"следующий канал {channel_number} из {len(channel_ids)}",
                phase="channels",
            )
            set_runtime(
                task_id,
                f"Связка {channel_number} из {len(channel_ids)}: {title_for_status}",
                activity=True,
            )
            resolved_linked_id = channel.get("linked_chat_id")
            resolved_linked_title = None

            def current_channel_allows_rpc(
                related_peer_id: int | None = None,
            ) -> bool:
                if pause_requested():
                    return False
                if strict_repository:
                    current_account_id = self._as_int(
                        worker_db.get_setting("telegram.account_id", 0), 0
                    )
                    if account_id <= 0 or current_account_id != account_id:
                        return False
                    if get_account_restriction_state(
                        worker_db, account_id=account_id
                    ).get("active"):
                        return False
                if callable(get_channel_by_id):
                    current = get_channel_by_id(channel_id, account_id=account_id)
                    if current is None:
                        return False
                    if isinstance(current, dict) and bool(
                        current.get("local_banned_at")
                    ):
                        return False
                checker = getattr(type(worker_db), "is_channel_locally_banned", None)
                if callable(checker):
                    if checker(worker_db, channel_id, account_id=account_id):
                        return False
                    if related_peer_id is not None and checker(
                        worker_db,
                        related_peer_id,
                        account_id=account_id,
                    ):
                        return False
                return True

            def create_join_dispatch_barrier(related_peer_id: int):
                factory = getattr(
                    type(self.queue_worker),
                    "create_scope_dispatch_barrier",
                    None,
                )
                if not callable(factory):
                    return None
                return factory(
                    self.queue_worker,
                    ("task", task_id),
                    ("channel", channel_id, account_id),
                    ("channel", related_peer_id, account_id),
                    pre_dispatch_check=lambda: current_channel_allows_rpc(
                        related_peer_id
                    ),
                )

            def commit_channel_ban(reason: str) -> bool:
                banner = getattr(type(worker_db), "ban_channel_locally", None)
                bound_banner = None
                if not callable(banner):
                    bound_banner = getattr(worker_db, "ban_channel_locally", None)
                    if not callable(bound_banner):
                        return False

                def mutation():
                    if callable(banner):
                        changed = banner(
                            worker_db,
                            channel_id,
                            reason,
                            related_peer_id=resolved_linked_id,
                            account_id=account_id,
                        )
                    else:
                        fallback_banner = cast(Any, bound_banner)
                        changed = fallback_banner(
                            channel_id,
                            reason,
                            related_peer_id=resolved_linked_id,
                            account_id=account_id,
                        )
                    if changed is False:
                        raise RuntimeError(
                            "Ambiguous Join target disappeared before local ban"
                        )
                    return bool(changed)

                runner = getattr(self.queue_worker, "cancel_scopes_and_run", None)
                scopes = [("channel", channel_id, account_id)]
                if resolved_linked_id is not None:
                    scopes.append(("channel", int(resolved_linked_id), account_id))
                if callable(runner):
                    return bool(runner(scopes, mutation))
                return bool(mutation())

            try:
                if not current_channel_allows_rpc():
                    if pause_requested():
                        pause_at_checkpoint(phase="channels")
                    banned_count += 1
                    channel_index += 1
                    persist_checkpoint(phase="channels")
                    continue
                linked_resolver = linked.get_linked_chat_id
                route_barrier = create_join_dispatch_barrier(channel_id)
                linked_id = await linked_resolver(
                    channel_id,
                    **dispatch_barrier_kwargs(linked_resolver, route_barrier),
                )
                if linked_id is None:
                    if strict_repository:
                        worker_db.update_channel_link(
                            channel_id,
                            None,
                            None,
                            "Нет чата обсуждения",
                            account_id=account_id,
                        )
                    else:
                        worker_db.update_channel_link(
                            channel_id, None, None, "Нет чата обсуждения"
                        )
                    set_runtime(
                        task_id,
                        f"Канал {channel_number} из {len(channel_ids)}: "
                        f"{title_for_status} · нет чата обсуждения",
                        activity=True,
                    )
                else:
                    linked_id = int(linked_id)
                    resolved_linked_id = linked_id
                    if linked_id in group_by_id:
                        # The discussion is already present in this account's
                        # dialog snapshot, so a JoinChannelRequest would be a
                        # redundant mutating RPC.
                        prepared_count += 1
                        status = "Связано · обсуждение уже в диалогах"
                    else:
                        if join_attempt_count > 0:
                            delay = random.uniform(join_delay_min, join_delay_max)
                            set_runtime(
                                task_id,
                                f"Пауза между вступлениями: {round(delay)} сек",
                                activity=True,
                            )
                            if not await self.queue_worker.safe_sleep(delay):
                                raise asyncio.CancelledError
                            if pause_requested():
                                pause_at_checkpoint(phase="channels")
                        set_runtime(
                            task_id,
                            f"Подготовка обсуждения {channel_number} из "
                            f"{len(channel_ids)}: {title_for_status}",
                            activity=True,
                        )
                        if pause_requested():
                            pause_at_checkpoint(phase="channels")
                        if not current_channel_allows_rpc(linked_id):
                            raise DeferredTelegramError(
                                "Local ban committed before Join dispatch",
                                code="local_ban_before_dispatch",
                                retry_after=1,
                            )
                        join_attempt_count += 1
                        join_kwargs = {}
                        join_barrier = create_join_dispatch_barrier(linked_id)
                        if join_barrier is not None:
                            join_kwargs["dispatch_barrier"] = join_barrier
                        newly_joined = bool(
                            await telegram.join_without_confirmation(
                                linked_id, **join_kwargs
                            )
                        )
                        prepared_count += 1
                        if newly_joined:
                            joined_count += 1
                            worker_db.record_join_event(
                                linked_id,
                                "joined",
                                account_id=account_id if account_id > 0 else None,
                            )
                            status = "Связано · вступление выполнено"
                        else:
                            status = "Связано · участие уже было"
                    if strict_repository:
                        worker_db.update_channel_link(
                            channel_id,
                            linked_id,
                            None,
                            status,
                            account_id=account_id,
                        )
                    else:
                        worker_db.update_channel_link(
                            channel_id, linked_id, None, status
                        )
                    if linked_id in group_by_id:
                        # Classify the group once in the local group phase. Keeping
                        # only the in-memory marker here avoids duplicate SQLite
                        # writes while the persisted channel checkpoint is enough
                        # to reconstruct this set after restart.
                        resolved_discussion_ids.add(linked_id)
                    set_runtime(
                        task_id,
                        f"Канал {channel_number} из {len(channel_ids)}: "
                        f"{title_for_status} · {status}",
                        activity=True,
                    )
            except asyncio.CancelledError:
                raise
            except DeferredTelegramError as exc:
                code = getattr(exc, "code", "")
                if code == "shutdown_before_dispatch":
                    pause_at_checkpoint(phase="channels")
                if code == "local_ban_before_dispatch":
                    if strict_repository and get_account_restriction_state(
                        worker_db, account_id=account_id
                    ).get("active"):
                        raise NonRetryableTelegramError(
                            "Telegram account is restricted before RPC dispatch",
                            code="user_restricted",
                        ) from exc
                    current_account_id = (
                        self._as_int(worker_db.get_setting("telegram.account_id", 0), 0)
                        if strict_repository
                        else account_id
                    )
                    if strict_repository and current_account_id != account_id:
                        raise NonRetryableTelegramError(
                            "Telegram account changed before RPC dispatch",
                            code="account_state_mismatch",
                        ) from exc
                    commit_channel_ban(
                        "Цель была локально заблокирована до отправки Join"
                    )
                    banned_count += 1
                    channel_index += 1
                    persist_checkpoint(phase="channels")
                    set_runtime(
                        task_id,
                        f"Канал {channel_number} из {len(channel_ids)}: "
                        f"{title_for_status} · локально заблокирован до Join; "
                        "очередь продолжена",
                        activity=True,
                        level="WARNING",
                    )
                    continue
                # The account-wide cooldown is still handled by QueueWorker, but
                # this particular target is never retried. Clear any partially
                # discovered link so an unconfirmed discussion cannot enter a
                # commenting campaign.
                if strict_repository:
                    worker_db.update_channel_link(
                        channel_id,
                        None,
                        None,
                        "Пропущено · Telegram FloodWait",
                        account_id=account_id,
                    )
                else:
                    worker_db.update_channel_link(
                        channel_id,
                        None,
                        None,
                        "Пропущено · Telegram FloodWait",
                    )
                worker_db.mark_link_checked(channel_id, account_id=account_id)
                channel_index += 1
                persist_checkpoint(phase="channels")
                set_runtime(
                    task_id,
                    f"Канал {channel_number} из {len(channel_ids)}: "
                    f"{title_for_status} · пропущен из-за FloodWait; повторной "
                    "проверки не будет",
                    activity=True,
                    level="WARNING",
                )
                raise
            except NonRetryableTelegramError as exc:
                code = getattr(exc, "code", "")
                if code == "join_result_unknown":
                    ban_reason = "Результат вступления неизвестен"
                    if not commit_channel_ban(ban_reason):
                        # Compatibility for minimal repository test doubles.
                        if strict_repository:
                            worker_db.update_channel_link(
                                channel_id,
                                None,
                                None,
                                "Заблокирован · результат вступления неизвестен",
                                account_id=account_id,
                            )
                        else:
                            worker_db.update_channel_link(
                                channel_id,
                                None,
                                None,
                                "Заблокирован · результат вступления неизвестен",
                            )
                        worker_db.mark_link_checked(channel_id, account_id=account_id)
                    banned_count += 1
                    channel_index += 1
                    persist_checkpoint(phase="channels")
                    log.warning(
                        "Channel locally banned after ambiguous Join: "
                        "account_id=%s channel_id=%s related_peer_id=%s",
                        account_id,
                        channel_id,
                        resolved_linked_id,
                    )
                    set_runtime(
                        task_id,
                        f"Канал {channel_number} из {len(channel_ids)}: "
                        f"{title_for_status} · заблокирован; очередь продолжена",
                        activity=True,
                        level="WARNING",
                    )
                    continue

                status = (
                    "Связано · заявка на вступление отправлена"
                    if code == "join_requested"
                    else f"Недоступно: {exc}"
                )
                if strict_repository:
                    worker_db.update_channel_link(
                        channel_id,
                        resolved_linked_id,
                        resolved_linked_title,
                        status,
                        account_id=account_id,
                    )
                else:
                    worker_db.update_channel_link(
                        channel_id,
                        resolved_linked_id,
                        resolved_linked_title,
                        status,
                    )
                set_runtime(
                    task_id,
                    f"Канал {channel_number} из {len(channel_ids)}: "
                    f"{title_for_status} · {status}",
                    activity=True,
                    level="WARNING",
                )
                if code in {
                    "peer_flood",
                    "user_restricted",
                    "user_banned",
                    "auth_key_duplicated",
                    "flood_wait_long",
                    "flood_wait_repeated",
                    "security_time_sync",
                }:
                    raise
            except TelegramOperationError:
                raise
            except Exception:
                log.exception("Could not link or prepare channel %s", channel_id)
                raise

            worker_db.mark_link_checked(channel_id, account_id=account_id)
            channel_index += 1
            persist_checkpoint(phase="channels")
            if pause_requested():
                raise TaskPausedError(
                    "Остановлено пользователем; прогресс связок сохранён"
                )

        if str(checkpoint.get("phase") or "") != "groups":
            persist_checkpoint(phase="groups")

        while group_index < len(group_ids):
            require_account_binding()
            if self.queue_worker.isInterruptionRequested():
                raise asyncio.CancelledError
            if pause_requested():
                pause_at_checkpoint(phase="groups")
            group_id = group_ids[group_index]
            group = group_by_id.get(group_id)
            if group is None:
                publish_activity(
                    f"Группа {group_index + 1} пропущена: удалена из списка во время ожидания"
                )
                group_index += 1
                persist_checkpoint(phase="groups")
                continue

            group_number = group_index + 1
            # Group classification is fully local. ``iter_dialogs`` already
            # supplied ``has_link`` and channel-side discovery marks any linked
            # discussion found during this same pass. No GetFullChannelRequest is
            # performed for ordinary groups.
            is_linked = (
                str(group.get("comment_mode") or "") == "linked_discussion"
                or group_id in resolved_discussion_ids
            )
            status = (
                "Связанное обсуждение · только комментарии к постам"
                if is_linked
                else "Группа · локально определена как обычная"
            )
            group_update_kwargs = {
                "is_linked": is_linked,
                "status": status,
            }
            if account_id > 0:
                group_update_kwargs["account_id"] = account_id
            worker_db.update_group_link_classification(
                group_id,
                **group_update_kwargs,
            )
            worker_db.mark_link_checked(group_id, account_id=account_id)
            group_index += 1
            persist_checkpoint(phase="groups")
            if group_number == len(group_ids) or group_number % 50 == 0:
                set_runtime(
                    task_id,
                    f"Локально классифицировано групп: {group_number} из "
                    f"{len(group_ids)}",
                    activity=True,
                )
            if pause_requested():
                raise TaskPausedError(
                    "Остановлено пользователем; прогресс связок сохранён"
                )

        require_account_binding()
        if strict_repository:
            worker_db.refresh_group_comment_modes(account_id=account_id)
        else:
            worker_db.refresh_group_comment_modes()
        set_runtime(
            task_id,
            "Связки подготовлены: "
            f"каналов {len(channel_ids)}, участие подтверждено {prepared_count}, "
            f"новых вступлений {joined_count}, локально заблокировано {banned_count}, "
            "обычные группы не используются для прямых сообщений",
            activity=True,
        )
        payload.pop("_link_checkpoint", None)
        final_checkpoint = worker_db.update_task_checkpoint(task_id, payload, 100)
        if final_checkpoint is False:
            raise RuntimeError("Could not finalize link task checkpoint")
        worker_db.update_task_progress(task_id, 100)

    return link_channels
