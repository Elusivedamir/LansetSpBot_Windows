from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, Slot

from core.account_state import has_pending_account_state
from core.account_restriction import get_account_restriction_state
from core.campaign_schedule import from_db_time, local_display, utc_now
from core.config import MAX_COMMENT_VARIANTS
from core.openai_settings import (
    DEFAULT_OPENAI_SYSTEM_PROMPT,
    SOURCE_OPENAI,
    CommentGenerationSettings,
    normalize_comment_source,
)

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class CommentCampaignAPIMixin(_MixinHost):
    _scheduler_error_present: bool
    _scheduler_failures: int
    _secret_migration_retry_at: float

    def start_comment_campaign(
        self,
        comments: list[str],
        *,
        continuous: bool = True,
        daily_limit: int | None = None,
        comment_source: str = "prepared",
    ):
        slots = [
            str(item).strip() if isinstance(item, str) else ""
            for item in comments[:MAX_COMMENT_VARIANTS]
        ]
        slots += [""] * (MAX_COMMENT_VARIANTS - len(slots))
        normalized: list[str] = []
        seen: set[str] = set()
        for item in slots:
            if not item or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        source = normalize_comment_source(comment_source)
        if not normalized:
            # OpenAI mode also needs the bag: one variant is drawn per send and
            # handed to the model as the meaning that must survive rewriting.
            raise ValueError("Добавьте хотя бы один комментарий")
        openai_public = dict(self.database.get_settings("openai."))
        openai_settings = CommentGenerationSettings.from_mapping(openai_public)
        openai_prompt = str(
            openai_public.get("openai.system_prompt")
            or DEFAULT_OPENAI_SYSTEM_PROMPT
        ).strip()
        if source == SOURCE_OPENAI:
            key_reader = getattr(self, "_strict_openai_key", None)
            if not callable(key_reader) or not str(key_reader() or "").strip():
                raise ValueError("Сначала сохраните API-ключ OpenAI")
            if not openai_prompt:
                raise ValueError("System-промпт OpenAI не задан")
        restriction = get_account_restriction_state(self.database)
        if restriction.get("active"):
            raise ValueError(
                "Отправки заблокированы после ограничения Telegram. "
                "Проверьте аккаунт через @SpamBot в живом журнале."
            )
        # Persist first, then read back the authoritative SQLite value.  This
        # guarantees that the campaign snapshot exactly matches the setting the
        # user selected before pressing Start.
        if daily_limit is not None:
            self.set_comment_daily_limit(daily_limit)
        limit = self.get_comment_daily_limit()
        if limit <= 0:
            raise ValueError("Выберите количество комментариев в сутки от 1 до 1000")
        account_id = int(self.database.get_setting("telegram.account_id", 0) or 0)
        if account_id <= 0:
            raise ValueError("Сначала авторизуйте Telegram-аккаунт")
        self.database.reconcile_comment_schedule()
        if self.database.get_active_comment_campaign():
            raise ValueError(
                "Кампания уже запущена. Остановите её перед созданием новой"
            )
        if self.database.get_active_join_campaign():
            raise ValueError("Сначала завершите кампанию вступлений")
        linked_count = self.database.count_channels_for_commenting(cooldown_hours=0)
        if linked_count <= 0:
            raise ValueError(
                "Нет готовых каналов или групп. Сначала получите список и выполните вкладку «Связки»"
            )
        eligible_count = self.database.count_channels_for_commenting(
            cooldown_hours=self.COMMENT_CHANNEL_COOLDOWN_HOURS
        )
        if eligible_count <= 0:
            raise ValueError(
                "Все доступные каналы и группы уже проверялись за последние 24 часа. "
                "Новые цели можно запустить отдельной кампанией."
            )
        effective_limit = min(limit, eligible_count)
        self.save_comment_profile(
            slots,
            visible_count=max(1, min(MAX_COMMENT_VARIANTS, len(comments) or 1)),
            account_id=account_id,
        )
        campaign = self.database.create_comment_campaign(
            normalized,
            daily_limit=limit,
            slot_count=effective_limit,
            duration_hours=self.campaign_hours,
            continuous=bool(continuous),
            allow_empty_comments=False,
            account_id=account_id,
        )
        campaign_id = int((campaign or {}).get("id") or 0)
        if campaign_id <= 0:
            raise RuntimeError("Кампания создана без корректного идентификатора")
        self.database.save_campaign_comment_settings(
            campaign_id=campaign_id,
            account_id=account_id,
            comment_source=source,
            settings=openai_settings,
            system_prompt=openai_prompt,
        )
        try:
            self.database.insert_log(
                "INFO",
                "Кампания комментирования создана: "
                f"выбрано пользователем={limit}; доступно целей={eligible_count}; "
                f"запланировано уникальных целей={effective_limit}; "
                f"темп ползунка={limit}/{self.campaign_hours}ч; "
                f"источник={source}; окно повторной проверки=24 ч",
                account_id=account_id,
            )
        except Exception:
            log.exception("Could not persist comment campaign start log")
        result = dict(campaign or {})
        result["requested_daily_limit"] = limit
        result["eligible_channel_count"] = eligible_count
        result["planned_count"] = effective_limit
        result["comment_source"] = source
        QTimer.singleShot(0, self._campaign_tick)
        return result

    def get_comment_campaign_state(self, account_id: int | None = None):
        owner_account_id = (
            self.get_current_account_id() if account_id is None else max(0, int(account_id))
        )
        if owner_account_id <= 0:
            return None
        campaign = (
            self.database.get_active_comment_campaign(account_id=owner_account_id)
            or self.database.get_latest_comment_campaign(account_id=owner_account_id)
        )
        if not campaign:
            return None
        summary = self.database.get_comment_schedule_summary(campaign["id"])
        result = dict(campaign)
        comment_settings = self.database.get_campaign_comment_settings(campaign["id"])
        result["comment_source"] = normalize_comment_source(
            comment_settings.get("comment_source")
        )
        result["schedule_counts"] = summary["counts"]
        result["planned_count"] = sum(
            int(value) for value in summary["counts"].values()
        )
        if str(campaign.get("status") or "") == "network_wait":
            result["next_scheduled_at"] = campaign.get("network_retry_at")
        else:
            result["next_scheduled_at"] = summary["next_scheduled_at"]
        result["next_scheduled_display"] = local_display(result["next_scheduled_at"])
        result["started_display"] = local_display(result.get("started_at"))
        result["ends_display"] = local_display(result.get("ends_at"))
        return result

    def get_comment_campaign_schedule(
        self, campaign_id: int | None = None, limit: int = 200
    ):
        if campaign_id is None:
            owner_account_id = self.get_current_account_id()
            if owner_account_id <= 0:
                return []
            campaign = (
                self.database.get_active_comment_campaign(account_id=owner_account_id)
                or self.database.get_latest_comment_campaign(account_id=owner_account_id)
            )
            if not campaign:
                return []
            campaign_id = int(campaign["id"])
        return self.database.get_comment_schedule(campaign_id, limit=limit)

    def pause_comment_campaign(self) -> bool:
        campaign = self.database.get_active_comment_campaign()
        if not campaign or campaign.get("status") != "running":
            return False
        campaign_id = int(campaign["id"])
        changed = self._cancel_scopes_and_mutate(
            (("comment_campaign", campaign_id),),
            lambda: self.database.pause_comment_campaign(campaign_id),
        )
        return bool(changed)

    def _continuous_comment_cycle_capacity(self, daily_limit: int) -> tuple[int, int]:
        """Return the desired and currently eligible slot count for a new cycle."""
        linked_count = self.database.count_channels_for_commenting(cooldown_hours=0)
        desired = min(max(1, int(daily_limit)), max(0, int(linked_count)))
        eligible = self.database.count_channels_for_commenting(
            cooldown_hours=self.COMMENT_CHANNEL_COOLDOWN_HOURS
        )
        return desired, max(0, int(eligible))

    def _start_next_continuous_comment_cycle(self, source: dict[str, Any]) -> bool:
        """Create exactly one full-capacity successor when its channels are ready."""
        comments = list(source.get("comments") or [])
        source_campaign_id = int(source.get("id") or 0)
        source_settings = self.database.get_campaign_comment_settings(source_campaign_id)
        comment_source = normalize_comment_source(source_settings.get("comment_source"))
        if not bool(source.get("continuous")):
            return False
        if not comments:
            return False
        limit = int(source.get("daily_limit") or self.max_channels_per_run)
        source_account_id = int(source.get("account_id") or 0)
        current_account_id = int(
            self.database.get_setting("telegram.account_id", 0) or 0
        )
        if source_account_id <= 0 or current_account_id != source_account_id:
            return False
        desired, eligible = self._continuous_comment_cycle_capacity(limit)
        if desired <= 0 or eligible < desired:
            return False
        successor = self.database.create_comment_campaign(
            comments,
            daily_limit=limit,
            slot_count=desired,
            duration_hours=self.campaign_hours,
            continuous=True,
            allow_empty_comments=False,
            account_id=source_account_id,
        )
        successor_id = int((successor or {}).get("id") or 0)
        if successor_id <= 0:
            return False
        snapshot_mapping = {
            "openai.model": source_settings.get("model"),
            "openai.max_words": source_settings.get("max_words"),
            "openai.temperature": source_settings.get("temperature"),
            "openai.timeout_seconds": source_settings.get("timeout_seconds"),
            "openai.max_generation_attempts": source_settings.get(
                "max_generation_attempts"
            ),
        }
        self.database.save_campaign_comment_settings(
            campaign_id=successor_id,
            account_id=source_account_id,
            comment_source=comment_source,
            settings=CommentGenerationSettings.from_mapping(snapshot_mapping),
            system_prompt=str(source_settings.get("system_prompt") or ""),
        )
        QTimer.singleShot(0, self._campaign_tick)
        return True

    def resume_comment_campaign(self) -> bool:
        if get_account_restriction_state(self.database).get("active"):
            raise ValueError(
                "Кампания не может быть продолжена до снятия ограничения через @SpamBot"
            )
        campaign = self.database.get_active_comment_campaign()
        if not campaign or campaign.get("status") != "paused":
            return False
        # A pause suspends the campaign clock; it never consumes pending slots.
        # Database.resume_comment_campaign() re-lays every pending slot from the
        # current moment and extends ends_at when the original window elapsed.
        resumed = self.database.resume_comment_campaign(campaign["id"])
        if resumed:
            self._clear_campaign_cancellation(campaign["id"])
            QTimer.singleShot(0, self._campaign_tick)
        return bool(resumed)

    def stop_comment_campaign(self) -> bool:
        campaign = self.database.get_active_comment_campaign()
        if not campaign:
            return False
        campaign_id = int(campaign["id"])
        changed = self._cancel_scopes_and_mutate(
            (("comment_campaign", campaign_id),),
            lambda: self.database.stop_comment_campaign(campaign_id),
        )
        return bool(changed)

    def set_auth_in_progress(self, active: bool) -> None:
        self._auth_in_progress = bool(active)
        if not self._auth_in_progress:
            QTimer.singleShot(0, self._campaign_tick)

    def _clear_scheduler_error(self) -> None:
        self._scheduler_failures = 0
        if not self._scheduler_error_present:
            return
        self.database.set_setting("scheduler.comment_error", "")
        self._scheduler_error_present = False

    def _record_scheduler_error(self, message: str) -> None:
        self.database.set_setting("scheduler.comment_error", str(message))
        self._scheduler_error_present = True

    @Slot()
    def _campaign_tick(self) -> None:
        # A queued Qt timer may fire after coordinated shutdown has begun.
        # Never create new non-daemon migration work once shutdown is requested.
        if self._shutdown_requested:
            return
        if self._secret_migration_required.is_set():
            if (
                not self.is_secret_migration_running()
                and time.monotonic() >= self._secret_migration_retry_at
            ):
                self._secret_migration_retry_at = time.monotonic() + 60.0
                self._secret_migration_thread = threading.Thread(
                    target=getattr(type(self), "_migrate_legacy_secrets"),
                    args=(
                        self.database,
                        self.secret_store,
                        self.SECRET_SETTING_KEYS,
                        self._secret_lock,
                        self._secret_migration_required,
                    ),
                    name="marlen-secret-migration-retry",
                    daemon=False,
                )
                self._secret_migration_thread.start()
            return
        if has_pending_account_state(self.database.path):
            return
        try:
            from services.multiaccount_scheduler import run_multiaccount_campaign_tick

            run_multiaccount_campaign_tick(self)
        except Exception as exc:
            self._scheduler_failures += 1
            log.exception("Persistent campaign scheduler failed")
            try:
                self._record_scheduler_error(f"{type(exc).__name__}: {exc}")
                if self._scheduler_failures >= 3:
                    campaign = self.database.get_active_comment_campaign()
                    if campaign and campaign.get("status") == "running":
                        self.database.pause_comment_campaign(
                            campaign["id"],
                            reason="Планировщик приостановлен после повторных ошибок",
                        )
                    join_campaign = self.database.get_active_join_campaign()
                    if join_campaign and join_campaign.get("status") == "running":
                        self.database.pause_join_campaign(
                            join_campaign["id"],
                            "Планировщик приостановлен после повторных ошибок",
                        )
            except Exception:
                log.exception("Could not persist scheduler failure")
        else:
            self._clear_scheduler_error()

    def _campaign_tick_once(self) -> None:
        now = utc_now()
        if self.database.has_due_pending_tasks():
            self.start_queue()
        self._join_campaign_tick(now)
        self.database.reconcile_comment_schedule()
        campaign = self.database.get_active_comment_campaign()
        if not campaign:
            latest = self.database.get_latest_comment_campaign()
            latest_end = from_db_time((latest or {}).get("ends_at"))
            if (
                latest
                and latest.get("status") == "completed"
                and bool(latest.get("continuous"))
                and latest_end is not None
                and latest_end <= now
            ):
                self._start_next_continuous_comment_cycle(latest)
            return
        status = str(campaign.get("status") or "")
        if status == "paused":
            return
        if status == "network_wait":
            retry_at = from_db_time(campaign.get("network_retry_at"))
            if retry_at is None or retry_at > now:
                return
            if not self.database.resume_network_wait_campaign(campaign["id"], now=now):
                return
            campaign = self.database.get_comment_campaign(campaign["id"]) or campaign
            status = str(campaign.get("status") or "")
        elif status == "running":
            current_end = from_db_time(campaign.get("ends_at"))
            self.database.redistribute_pending_comment_slots(
                campaign["id"],
                now=now,
                grace_seconds=180,
                force=bool(current_end is not None and current_end <= now),
            )
            campaign = self.database.get_comment_campaign(campaign["id"]) or campaign
            status = str(campaign.get("status") or "")

        end = from_db_time(campaign.get("ends_at"))
        if end is not None and end <= now:
            comments = list(campaign.get("comments") or [])
            continuous = bool(campaign.get("continuous"))
            limit = int(campaign.get("daily_limit") or self.max_channels_per_run)
            was_running = status in {"running", "network_wait", "cycle_wait"}
            source_settings = self.database.get_campaign_comment_settings(campaign["id"])
            source_mode = normalize_comment_source(source_settings.get("comment_source"))
            can_continue = source_mode == SOURCE_OPENAI or bool(comments)
            if continuous and was_running and can_continue:
                desired, eligible = self._continuous_comment_cycle_capacity(limit)
                if desired > 0 and eligible < desired:
                    return
            self.database.complete_comment_campaign(campaign["id"])
            if continuous and was_running and can_continue:
                self._start_next_continuous_comment_cycle(campaign)
            return
        if status != "running":
            return

        pending_campaign_task = self.database.has_pending_task_type("auto_comment_slot")
        if pending_campaign_task:
            if self.database.has_due_pending_task_type("auto_comment_slot"):
                self.start_queue()
            return

        queued = self.database.queue_due_comment_slot(now=now)
        if queued is not None:
            self.start_queue()
