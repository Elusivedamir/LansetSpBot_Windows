from __future__ import annotations

import logging
from typing import Any

from services.api_parts.comments import CommentCampaignAPIMixin
from services.api_parts.joins import JoinCampaignAPIMixin
from storage.account_database_view import AccountDatabaseView

log = logging.getLogger(__name__)


class AccountCampaignContext(
    CommentCampaignAPIMixin,
    JoinCampaignAPIMixin,
):
    """Run existing campaign scheduling methods against one account view."""

    def __init__(self, root, account_id: int) -> None:
        self.root = root
        self.account_id = int(account_id)
        self.database = AccountDatabaseView(root.database, self.account_id)
        self.queue_worker = root.queue_worker
        self.max_channels_per_run = root.max_channels_per_run
        self.max_joins_per_hour = root.max_joins_per_hour
        self.campaign_hours = root.campaign_hours
        self.COMMENT_CHANNEL_COOLDOWN_HOURS = root.COMMENT_CHANNEL_COOLDOWN_HOURS
        self._scheduler_error_present = False
        self._scheduler_failures = 0
        self._secret_migration_retry_at = 0.0

    def get_current_account_id(self) -> int:
        return self.account_id

    def start_queue(self) -> bool:
        return bool(self.root.start_queue())

    def _cancel_scopes_and_mutate(self, scopes, mutation):
        return self.root._cancel_scopes_and_mutate(scopes, mutation)

    def _clear_scope_cancellation(self, scope_type, scope_id):
        return self.root._clear_scope_cancellation(scope_type, scope_id)

    def _campaign_tick(self) -> None:
        # QTimer callbacks belong to the root ServiceAPI.
        return None

    def _strict_openai_key(self) -> str | None:
        return self.root._strict_account_secret(
            self.account_id, "openai.api_key"
        )

    def set_comment_daily_limit(self, value: int) -> int:
        normalized = max(0, min(1000, int(value)))
        self.database.set_setting(
            self.root.COMMENT_DAILY_LIMIT_SETTING, normalized
        )
        return normalized

    def get_comment_daily_limit(self) -> int:
        raw = self.database.get_setting(
            self.root.COMMENT_DAILY_LIMIT_SETTING,
            self.max_channels_per_run,
        )
        try:
            return max(0, min(1000, int(raw)))
        except (TypeError, ValueError, OverflowError):
            return int(self.max_channels_per_run)


def run_multiaccount_campaign_tick(root) -> dict[int, str]:
    """Schedule every runnable account independently and isolate failures."""

    outcomes: dict[int, str] = {}
    for account in root.database.list_telegram_accounts():
        account_id = int(account.get("telegram_account_id") or 0)
        if account_id <= 0:
            continue
        state = str(account.get("runtime_state") or "")
        if bool(account.get("stopped")) or state in {
            "stopping",
            "stopped",
            "authorization_required",
            "error",
        }:
            outcomes[account_id] = "skipped"
            continue
        context = AccountCampaignContext(root, account_id)
        try:
            context._campaign_tick_once()
        except Exception as exc:
            outcomes[account_id] = f"error:{type(exc).__name__}"
            log.exception(
                "Persistent campaign scheduler failed for account %s",
                account_id,
            )
            try:
                context.database.set_setting(
                    "scheduler.comment_error",
                    f"{type(exc).__name__}: {exc}",
                )
                campaign = context.database.get_active_comment_campaign(
                    account_id=account_id
                )
                if campaign and campaign.get("status") == "running":
                    context.database.pause_comment_campaign(
                        campaign["id"],
                        reason=(
                            "Планировщик аккаунта приостановлен после ошибки"
                        ),
                    )
                join_campaign = context.database.get_active_join_campaign(
                    account_id=account_id
                )
                if join_campaign and join_campaign.get("status") == "running":
                    context.database.pause_join_campaign(
                        join_campaign["id"],
                        "Планировщик аккаунта приостановлен после ошибки",
                    )
            except Exception:
                log.exception(
                    "Could not persist scheduler failure for account %s",
                    account_id,
                )
        else:
            outcomes[account_id] = "ok"
            try:
                context.database.set_setting("scheduler.comment_error", "")
            except Exception:
                log.exception(
                    "Could not clear scheduler error for account %s",
                    account_id,
                )
    return outcomes
