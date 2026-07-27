from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast

from core.exceptions import (
    NonRetryableTelegramError,
)
from core.rate_limiter import RateLimiter
from core.activity_schedule import ActivityScheduleManager
from core.openai_settings import (
    DEFAULT_OPENAI_SYSTEM_PROMPT,
    OPENAI_API_KEY_SECRET,
    CommentGenerationSettings,
)
from services.import_service import ImportValidationError
from services.openai_comment_service import OpenAICommentService
from workers.handlers import (
    create_comment_slot_handler,
    create_join_slot_handler,
    create_manual_comment_handler,
)
from workers.handlers.link_channels import create_link_channels_handler

log = logging.getLogger(__name__)


def create_worker_handlers(
    self,
    *,
    TelegramService,
    ImportService,
    LinkedChatService,
    CommentService,
):
    worker_db = self.queue_worker.get_db()

    async def noop(task: dict[str, Any]) -> None:
        await asyncio.sleep(0)

    importer = ImportService(worker_db)

    async def import_data(task: dict[str, Any]) -> None:
        payload = task.get("payload") or {}
        files = payload.get("files")
        if (
            files is None
            and payload.get("kind") is not None
            and payload.get("path") is not None
        ):
            files = {str(payload["kind"]): payload["path"]}
        if not isinstance(files, dict):
            raise NonRetryableTelegramError(
                "import requires a files mapping", code="invalid_payload"
            )
        try:
            report = importer.migrate(cast(dict[str, str | Path], files))
        except ImportValidationError as exc:
            raise NonRetryableTelegramError(str(exc), code="invalid_payload") from exc
        if report["errors"]:
            details = "; ".join(
                f"{item['kind']}: {item['error']}" for item in report["errors"]
            )
            raise NonRetryableTelegramError(details, code="import_failed")

    api = getattr(self, "api", None)
    secret_lock = getattr(api, "_secret_lock", None)
    lock_context = secret_lock if secret_lock is not None else nullcontext()
    try:
        with lock_context:
            settings = self._telegram_settings(worker_db)
    except RuntimeError as exc:
        secret_error = str(exc)

        async def secret_store_unavailable(task: dict[str, Any]) -> None:
            from core.exceptions import DeferredTelegramError

            raise DeferredTelegramError(
                f"Защищённое хранилище временно недоступно: {secret_error}",
                retry_after=120,
                code="secret_store_unavailable",
            )

        return {
            "noop": noop,
            "import": import_data,
            "sync_channels": secret_store_unavailable,
            "link_channels": secret_store_unavailable,
            "auto_comment": secret_store_unavailable,
            "auto_comment_slot": secret_store_unavailable,
            "direct_message": secret_store_unavailable,
            "comment": secret_store_unavailable,
            "sync_saved_dialogs": secret_store_unavailable,
            "join_saved_slot": secret_store_unavailable,
            "openai_test": secret_store_unavailable,
        }, None

    def openai_api_key_provider() -> str | None:
        active_lock = secret_lock if secret_lock is not None else nullcontext()
        with active_lock:
            value = self._strict_secret_value(OPENAI_API_KEY_SECRET)
            return None if value is None else str(value)

    openai_service = OpenAICommentService(
        openai_api_key_provider,
        semaphore=asyncio.Semaphore(2),
    )

    async def openai_test(task: dict[str, Any]) -> dict[str, Any]:
        payload = task.get("payload") or {}
        public = worker_db.get_settings("openai.")
        generation_settings = CommentGenerationSettings.from_mapping(public)
        system_prompt = str(
            public.get("openai.system_prompt") or DEFAULT_OPENAI_SYSTEM_PROMPT
        ).strip()
        generated = await openai_service.test_connection(
            system_prompt,
            generation_settings,
            post_text=str(payload.get("post_text") or "").strip() or None,
        )
        return {
            "text": generated.text,
            "model": generated.model,
            "created_at": generated.created_at.isoformat(),
            "input_length": generated.input_length,
            "output_length": generated.output_length,
        }

    if not settings.configured:

        async def telegram_not_configured(task: dict[str, Any]) -> None:
            raise NonRetryableTelegramError(
                "Сначала подключите Telegram-аккаунт во вкладке «Аккаунт»",
                code="telegram_not_configured",
            )

        return {
            "noop": noop,
            "import": import_data,
            "sync_channels": telegram_not_configured,
            "link_channels": telegram_not_configured,
            "auto_comment": telegram_not_configured,
            "auto_comment_slot": telegram_not_configured,
            "direct_message": telegram_not_configured,
            "comment": telegram_not_configured,
            "sync_saved_dialogs": telegram_not_configured,
            "join_saved_slot": telegram_not_configured,
            "openai_test": openai_test,
        "telegram_health": telegram_health,
        }, None

    limiter = RateLimiter(self.config.rate_limit)
    runtime: dict[str, Any] = {
        "task_id": None,
        "account_id": 0,
        "prefix": "",
        "last_activity": "",
    }

    def publish_activity(
        text: str, *, level: str = "INFO", category: str = "Связки"
    ) -> None:
        """Persist one user-facing activity line without affecting the task.

        The live journal reads the ``logs`` table. Runtime ``status_text`` is
        intentionally transient and can be replaced several times between GUI
        refreshes, so important milestones are mirrored here. Logging is
        best-effort: a journal write must never interrupt a Telegram operation
        or corrupt its checkpoint.
        """

        clean = " ".join(str(text or "").split())
        if not clean:
            return
        clean_category = " ".join(str(category or "Журнал").split()) or "Журнал"
        rendered = f"[{clean_category}] {clean}"
        fingerprint = f"{str(level or 'INFO').upper()}:{rendered}"
        if fingerprint == runtime.get("last_activity"):
            return
        runtime["last_activity"] = fingerprint
        try:
            worker_db.insert_log(
                str(level or "INFO").upper(),
                rendered,
                account_id=int(runtime.get("account_id") or 0),
            )
        except Exception:
            log.exception("Could not persist activity journal event")

    def publish_runtime_status(text: str) -> None:
        task_id = runtime.get("task_id")
        if task_id is None:
            return
        prefix = str(runtime.get("prefix") or "").strip()
        body = str(text or "").strip()
        if prefix and body:
            body = f"{prefix} · {body}"
        elif prefix:
            body = prefix
        worker_db.update_task_status_text(int(task_id), body)

    def set_runtime(
        task_id: int,
        prefix: str = "",
        *,
        activity: bool = False,
        level: str = "INFO",
        account_id: int | None = None,
    ) -> None:
        runtime["task_id"] = int(task_id)
        if account_id is not None:
            runtime["account_id"] = max(0, int(account_id or 0))
        runtime["prefix"] = str(prefix or "").strip()
        publish_runtime_status("")
        if activity:
            publish_activity(prefix, level=level)

    telegram = TelegramService(
        settings, limiter, status_callback=publish_runtime_status
    )
    linked = LinkedChatService(telegram)
    class _DatabaseActivitySchedule:
        @staticmethod
        def require_active():
            schedule_values = worker_db.get_settings("automation.")
            schedule_account_id = self._as_int(
                worker_db.get_setting("telegram.account_id", 0), 0
            )
            try:
                manager = ActivityScheduleManager.from_mapping(
                    schedule_values,
                    account_id=schedule_account_id,
                )
            except ValueError as exc:
                # Invalid persisted schedule fails closed for automated comments
                # while leaving account/channel tools available for repair.
                schedule_error = str(exc)
                log.error("Invalid automation schedule: %s", schedule_error)
                raise NonRetryableTelegramError(
                    f"Некорректное локальное расписание: {schedule_error}",
                    code="activity_schedule_invalid",
                ) from exc
            return manager.require_active()

    activity_schedule = _DatabaseActivitySchedule()
    comments = CommentService(
        telegram,
        linked,
        worker_db,
        activity_schedule=activity_schedule,
    )

    async def sync_channels(task: dict[str, Any]) -> None:
        """Refresh working targets and the portable saved list in one dialog pass."""
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
                "Задача синхронизации принадлежит другому Telegram-аккаунту",
                code="account_state_mismatch",
                details={
                    "task_account_id": task_account_id,
                    "current_account_id": selected_account_id,
                },
            )
        phone = str(worker_db.get_setting("telegram.phone", "") or "")

        def require_account_binding() -> None:
            if not strict_repository:
                return
            current_account_id = self._as_int(
                worker_db.get_setting("telegram.account_id", 0), 0
            )
            if current_account_id != account_id:
                raise NonRetryableTelegramError(
                    "Telegram-аккаунт изменён во время синхронизации; "
                    "частичный результат сохранён только в исходном профиле",
                    code="account_state_mismatch",
                    details={
                        "task_account_id": account_id,
                        "current_account_id": current_account_id,
                    },
                )

        set_runtime(
            task_id,
            "Получение каналов, групп и сохранённого списка",
            account_id=account_id,
        )
        publish_activity(
            "Начато получение списка каналов и групп", category="Каналы"
        )

        channel_batch: list[dict[str, Any]] = []
        saved_batch: list[dict[str, Any]] = []
        seen_channel_ids: list[int] = []
        seen_dialog_ids: list[int] = []
        channel_count = 0
        saved_count = 0
        scanned_count = 0

        async def snapshots():
            async for item in telegram.iter_dialog_snapshot():
                yield item

        async def flush_channels() -> None:
            if channel_batch:
                require_account_binding()
                if strict_repository:
                    worker_db.upsert_channels_batch(
                        channel_batch, account_id=account_id
                    )
                else:  # pragma: no cover - compatibility for lightweight fakes
                    worker_db.upsert_channels_batch(channel_batch)
                channel_batch.clear()

        async def flush_saved() -> None:
            if not saved_batch or account_id <= 0:
                saved_batch.clear()
                return
            require_account_binding()
            saved_ids = worker_db.upsert_saved_dialogs_batch(
                saved_batch, account_id=account_id, phone=phone
            )
            if saved_ids:
                seen_dialog_ids.extend(int(value) for value in saved_ids)
            saved_batch.clear()

        def should_publish_progress(count: int) -> bool:
            # Small accounts must still visibly advance; large accounts should
            # not flood the live journal with hundreds of nearly identical rows.
            if count in {1, 10}:
                return True
            if count <= 100:
                return count % 25 == 0
            return count % 100 == 0

        try:
            async for snapshot in snapshots():
                if self.queue_worker.isInterruptionRequested():
                    raise asyncio.CancelledError
                scanned_count += 1
                channel = snapshot.get("work_target")
                if channel is not None:
                    channel_id = channel.get("id")
                    if channel_id is not None:
                        seen_channel_ids.append(int(channel_id))
                        channel_batch.append(
                            {
                                "channel_id": channel_id,
                                "title": channel.get("title"),
                                "username": channel.get("username"),
                                "target_kind": channel.get("target_kind", "channel"),
                                "comment_mode": channel.get(
                                    "comment_mode", "channel_post"
                                ),
                                "linked_chat_id": channel.get("linked_chat_id"),
                                "linked_chat_title": channel.get(
                                    "linked_chat_title"
                                ),
                                "link_status": channel.get("link_status"),
                                "access_hash": channel.get("access_hash"),
                                "peer_type": channel.get("peer_type"),
                            }
                        )
                        channel_count += 1
                saved = snapshot.get("saved_dialog")
                if saved is not None and account_id > 0:
                    saved_batch.append(saved)
                    saved_count += 1

                if len(channel_batch) >= 200:
                    await flush_channels()
                if len(saved_batch) >= 200:
                    await flush_saved()
                if should_publish_progress(scanned_count):
                    status = (
                        f"Обработано каналов и групп: {scanned_count} · "
                        f"рабочих целей: {channel_count} · сохранено: {saved_count}"
                    )
                    set_runtime(task_id, status)
                    publish_activity(status, category="Каналы")
                    worker_db.update_task_progress(
                        task_id, min(95, 5 + scanned_count // 10)
                    )

            await flush_channels()
            await flush_saved()
            require_account_binding()
            if strict_repository:
                worker_db.prune_channels_except(
                    seen_channel_ids, account_id=account_id
                )
            else:  # pragma: no cover - compatibility for lightweight fakes
                worker_db.prune_channels_except(seen_channel_ids)
            if account_id > 0:
                worker_db.mark_unseen_saved_dialogs_left(
                    account_id=account_id, seen_dialog_ids=seen_dialog_ids
                )
            final_status = (
                f"Список обновлён · найдено каналов и групп: {saved_count} · "
                f"рабочих целей: {channel_count}"
            )
            set_runtime(task_id, final_status)
            publish_activity(final_status, category="Каналы")
            worker_db.update_task_progress(task_id, 100)
        except asyncio.CancelledError:
            publish_activity(
                f"Получение списка отменено · обработано: {scanned_count}",
                level="WARNING",
                category="Каналы",
            )
            raise
        except Exception as exc:
            publish_activity(
                f"Ошибка получения списка после {scanned_count} элементов: "
                f"{type(exc).__name__}: {exc}",
                level="ERROR",
                category="Каналы",
            )
            raise

    link_channels = create_link_channels_handler(
        self=self,
        telegram=telegram,
        worker_db=worker_db,
        linked=linked,
        set_runtime=set_runtime,
        publish_activity=publish_activity,
    )

    async def sync_saved_dialogs(task: dict[str, Any]) -> None:
        """Backward-compatible alias for the unified one-pass synchronization."""
        await sync_channels(task)

    join_saved_slot = create_join_slot_handler(
        as_int=self._as_int,
        queue_worker=self.queue_worker,
        config=self.config,
        worker_db=worker_db,
        telegram=telegram,
        set_runtime=set_runtime,
    )

    async def direct_message(_task: dict[str, Any]) -> None:
        # Compatibility handler for old databases only. Marlen comments below
        # channel posts and never sends unrelated plain messages to groups/users.
        raise NonRetryableTelegramError(
            "Прямая отправка сообщений в обычные группы отключена",
            code="direct_group_disabled",
        )

    comment = create_manual_comment_handler(
        as_int=self._as_int,
        queue_worker=self.queue_worker,
        config=self.config,
        worker_db=worker_db,
        telegram=telegram,
        comments=comments,
    )

    auto_comment_slot = create_comment_slot_handler(
        as_int=self._as_int,
        queue_worker=self.queue_worker,
        config=self.config,
        worker_db=worker_db,
        telegram=telegram,
        comments=comments,
        openai_service=openai_service,
        set_runtime=set_runtime,
    )

    async def legacy_auto_comment_disabled(task: dict[str, Any]) -> None:
        raise NonRetryableTelegramError(
            "Пакетный режим отключён. Используйте суточную кампанию в GUI.",
            code="legacy_batch_disabled",
        )

    async def telegram_health(_task: dict[str, Any]) -> dict[str, Any]:
        await telegram.connect()
        client = getattr(telegram, "client", None)
        if client is None:
            raise NonRetryableTelegramError(
                "Telegram client is unavailable", code="handler_missing"
            )
        if not await client.is_user_authorized():
            raise NonRetryableTelegramError(
                "Telegram session requires authorization",
                code="authorization_required",
            )
        me = await client.get_me()
        actual_id = int(getattr(me, "id", 0) or 0)
        expected_id = int(getattr(telegram, "account_id", 0) or 0)
        if actual_id <= 0 or (expected_id > 0 and actual_id != expected_id):
            raise NonRetryableTelegramError(
                "Telegram session belongs to another account",
                code="account_state_mismatch",
                details={
                    "expected_account_id": expected_id,
                    "actual_account_id": actual_id,
                },
            )
        return {
            "account_id": actual_id,
            "authorized": True,
            "connected": True,
        }

    handlers = {
        "noop": noop,
        "import": import_data,
        "sync_channels": sync_channels,
        "link_channels": link_channels,
        "auto_comment": legacy_auto_comment_disabled,
        "auto_comment_slot": auto_comment_slot,
        "direct_message": direct_message,
        "comment": comment,
        "sync_saved_dialogs": sync_saved_dialogs,
        "join_saved_slot": join_saved_slot,
        "openai_test": openai_test,
    }
    return handlers, telegram.disconnect
