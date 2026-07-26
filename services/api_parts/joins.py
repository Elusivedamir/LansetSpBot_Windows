from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from PySide6.QtCore import QTimer

from core.campaign_schedule import from_db_time, local_display, utc_now
from core.account_restriction import get_account_restriction_state

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class JoinCampaignAPIMixin(_MixinHost):
    def get_saved_dialogs(self, account_id: int | None = None):
        owner_account_id = (
            self.get_current_account_id() if account_id is None else max(0, int(account_id))
        )
        return self.database.get_saved_dialogs(owner_account_id or None)

    def start_join_campaign(self):
        if get_account_restriction_state(self.database).get("active"):
            raise ValueError(
                "Вступления заблокированы после ограничения Telegram. "
                "Проверьте аккаунт через @SpamBot в живом журнале."
            )
        account_id = int(self.database.get_setting("telegram.account_id", 0) or 0)
        if account_id <= 0:
            raise ValueError("Сначала авторизуйте новый Telegram-аккаунт")
        if self.database.get_active_comment_campaign():
            raise ValueError("Сначала остановите кампанию комментирования")
        campaign = self.database.create_join_campaign(
            account_id, max_per_hour=self.max_joins_per_hour
        )
        QTimer.singleShot(0, self._campaign_tick)
        return campaign

    def get_join_campaign_state(self, account_id: int | None = None):
        owner_account_id = (
            self.get_current_account_id() if account_id is None else max(0, int(account_id))
        )
        if owner_account_id <= 0:
            return None
        campaign = (
            self.database.get_active_join_campaign(account_id=owner_account_id)
            or self.database.get_latest_join_campaign(account_id=owner_account_id)
        )
        if not campaign:
            return None
        summary = self.database.get_join_schedule_summary(campaign["id"])
        result = dict(campaign)
        result["schedule_counts"] = summary["counts"]
        result["next_scheduled_at"] = summary["next_scheduled_at"]
        result["next_scheduled_display"] = local_display(
            result.get("next_scheduled_at")
        )
        return result

    def pause_join_campaign(self):
        campaign = self.database.get_active_join_campaign()
        if not campaign or campaign.get("status") != "running":
            return False
        campaign_id = int(campaign["id"])
        return self._cancel_scopes_and_mutate(
            (("join_campaign", campaign_id),),
            lambda: self.database.pause_join_campaign(
                campaign_id, "Пауза пользователя"
            ),
        )

    def resume_join_campaign(self):
        if get_account_restriction_state(self.database).get("active"):
            raise ValueError(
                "Вступления нельзя продолжить до снятия ограничения через @SpamBot"
            )
        campaign = self.database.get_active_join_campaign()
        if not campaign or campaign.get("status") not in {"paused", "network_wait"}:
            return False
        changed = self.database.resume_join_campaign(campaign["id"])
        if changed:
            self._clear_scope_cancellation("join_campaign", campaign["id"])
            QTimer.singleShot(0, self._campaign_tick)
        return changed

    def stop_join_campaign(self):
        campaign = self.database.get_active_join_campaign()
        if not campaign:
            return False
        campaign_id = int(campaign["id"])
        return self._cancel_scopes_and_mutate(
            (("join_campaign", campaign_id),),
            lambda: self.database.stop_join_campaign(campaign_id),
        )

    def get_scheduler_error(self):
        return str(self.database.get_setting("scheduler.comment_error", "") or "")

    def _join_campaign_tick(self, now=None):
        now = now or utc_now()
        self.database.reconcile_join_schedule()
        campaign = self.database.get_active_join_campaign()
        if not campaign:
            return
        status = str(campaign.get("status") or "")
        if status == "network_wait":
            retry_at = from_db_time(campaign.get("network_retry_at"))
            if retry_at is None or retry_at > now:
                return
            if not self.database.resume_join_campaign(campaign["id"]):
                return
            campaign = self.database.get_join_campaign(campaign["id"]) or campaign
            status = str(campaign.get("status") or "")
        if status != "running":
            return
        summary = self.database.get_join_schedule_summary(campaign["id"])
        if summary["counts"] and summary["open_count"] == 0:
            self.database.complete_join_campaign(
                campaign["id"], "Кампания вступлений завершена"
            )
            return
        self.database.redistribute_pending_join_slots(campaign["id"], now=now)
        if self.database.has_pending_task_type("join_saved_slot"):
            if self.database.has_due_pending_task_type("join_saved_slot"):
                self.start_queue()
            return
        queued = self.database.queue_due_join_slot(now=now)
        if queued:
            self.start_queue()
