from __future__ import annotations

import logging
from typing import cast

from core.campaign_schedule import to_db_time, utc_now
from services.api_parts.comments import CommentCampaignAPIMixin
from services.api_parts.joins import JoinCampaignAPIMixin
from storage.account_database_view import AccountDatabaseView
from storage.db_common import DatabaseError, json_dumps_safe
from storage.sqlcipher_driver import dbapi as sqlite3

log = logging.getLogger(__name__)


class AccountCampaignDatabaseView(AccountDatabaseView):
    """Account-bound queue facade used by the persistent campaign scheduler.

    The domain repositories are already account-scoped, but the historical slot
    queue helpers used global ``tasks`` checks and selected the oldest campaign
    from every account.  This view keeps those operations inside the account that
    owns the current scheduler tick and writes the authoritative ``tasks.account_id``
    at insertion time.
    """

    _ACTIVE_TASK_STATUSES = ("pending", "running", "processing", "paused")

    def has_pending_task_type(self, task_type: str) -> bool:
        with self.get_connection() as conn:
            row = conn.execute(
                """SELECT 1 FROM tasks
                   WHERE account_id=? AND type=?
                     AND status IN ('pending','running','processing','paused')
                   LIMIT 1""",
                (self.account_id, str(task_type)),
            ).fetchone()
        return row is not None

    def has_due_pending_task_type(self, task_type: str) -> bool:
        with self.get_connection() as conn:
            row = conn.execute(
                """SELECT 1 FROM tasks
                   WHERE account_id=? AND type=? AND status='pending'
                     AND (not_before IS NULL OR not_before<=CURRENT_TIMESTAMP)
                   LIMIT 1""",
                (self.account_id, str(task_type)),
            ).fetchone()
        return row is not None

    def has_due_pending_tasks(self) -> bool:
        with self.get_connection() as conn:
            row = conn.execute(
                """SELECT 1 FROM tasks
                   WHERE account_id=? AND status='pending'
                     AND (not_before IS NULL OR not_before<=CURRENT_TIMESTAMP)
                   LIMIT 1""",
                (self.account_id,),
            ).fetchone()
        return row is not None

    def reconcile_comment_schedule(self, account_id=None):
        """Reconcile only comment schedule rows owned by this account."""
        del account_id
        return self._base.reconcile_comment_schedule(account_id=self.account_id)

    def reconcile_join_schedule(self, account_id=None):
        """Reconcile only join schedule rows owned by this account."""
        del account_id
        return self._base.reconcile_join_schedule(account_id=self.account_id)

    def _queue_due_campaign_slot(
        self,
        *,
        schedule_table: str,
        campaign_table: str,
        task_type: str,
        due_sql: str,
        due_params: tuple[object, ...],
        scope_error: str,
        database_error: str,
    ):
        """Atomically queue one due slot for this account.

        Only the two internal campaign specifications are accepted. SQL values
        remain bound parameters; the validated table names are fixed repository
        identifiers and never originate from user input.
        """
        allowed_specs = {
            ("comment_schedule", "comment_campaigns", "auto_comment_slot"),
            ("join_schedule", "join_campaigns", "join_saved_slot"),
        }
        if (schedule_table, campaign_table, task_type) not in allowed_specs:
            raise ValueError("Unsupported account campaign slot specification")

        connection = None
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.sqlite_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("BEGIN IMMEDIATE")

            active_task = connection.execute(
                f"""SELECT 1
                    FROM {schedule_table} s
                    JOIN {campaign_table} c ON c.id=s.campaign_id
                    JOIN tasks t ON t.id=s.task_id
                    WHERE c.account_id=?
                      AND s.status IN ('queued','running')
                      AND t.status IN ('pending','running','processing')
                    LIMIT 1""",
                (self.account_id,),
            ).fetchone()
            if active_task is not None:
                connection.commit()
                return None

            row = connection.execute(due_sql, due_params).fetchone()
            if row is None:
                connection.commit()
                return None

            owner = int(row["account_id"] or 0)
            if owner != self.account_id:
                raise DatabaseError(scope_error)

            payload = json_dumps_safe(
                {
                    "campaign_id": int(row["campaign_id"]),
                    "slot_id": int(row["slot_id"]),
                    "account_id": owner,
                }
            )
            cursor = connection.execute(
                """INSERT INTO tasks(
                       account_id, type, payload, status, progress, max_retries,
                       created_at, updated_at)
                   VALUES(?, ?, ?, 'pending', 0, 0,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (owner, task_type, payload),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("SQLite did not return a task id")
            task_id = int(cursor.lastrowid)

            updated = connection.execute(
                f"""UPDATE {schedule_table}
                    SET status='queued', task_id=?
                    WHERE id=? AND campaign_id=? AND status='pending'""",
                (task_id, int(row["slot_id"]), int(row["campaign_id"])),
            )
            if updated.rowcount != 1:
                connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                connection.rollback()
                return None
            connection.commit()
            return {
                "task_id": task_id,
                "campaign_id": int(row["campaign_id"]),
                "slot_id": int(row["slot_id"]),
                "account_id": owner,
            }
        except sqlite3.Error as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            raise DatabaseError(f"{database_error}: {exc}") from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    def queue_due_comment_slot(self, *, now=None):
        """Atomically queue one due comment slot owned by this account."""
        now = now or utc_now()
        now_text = to_db_time(now)
        return self._queue_due_campaign_slot(
            schedule_table="comment_schedule",
            campaign_table="comment_campaigns",
            task_type="auto_comment_slot",
            due_sql="""SELECT s.id AS slot_id, s.campaign_id, c.account_id
                       FROM comment_schedule s
                       JOIN comment_campaigns c ON c.id=s.campaign_id
                       WHERE c.account_id=?
                         AND c.status='running'
                         AND s.status='pending'
                         AND s.scheduled_at<=?
                         AND c.ends_at>?
                       ORDER BY s.scheduled_at ASC, s.id ASC
                       LIMIT 1""",
            due_params=(self.account_id, now_text, now_text),
            scope_error="Comment slot escaped its account scheduler scope",
            database_error="Failed to queue account-scoped campaign slot",
        )

    def queue_due_join_slot(self, *, now=None):
        """Atomically queue one due join slot owned by this account."""
        now = now or utc_now()
        now_text = to_db_time(now)
        return self._queue_due_campaign_slot(
            schedule_table="join_schedule",
            campaign_table="join_campaigns",
            task_type="join_saved_slot",
            due_sql="""SELECT s.id AS slot_id, s.campaign_id, c.account_id
                       FROM join_schedule s
                       JOIN join_campaigns c ON c.id=s.campaign_id
                       JOIN saved_dialogs d ON d.id=s.saved_dialog_id
                       WHERE c.account_id=?
                         AND c.status='running'
                         AND s.status='pending'
                         AND s.scheduled_at<=?
                         AND NOT EXISTS(
                             SELECT 1 FROM local_ban_targets b
                             WHERE b.account_id=c.account_id
                               AND b.peer_id=d.peer_id
                         )
                       ORDER BY s.scheduled_at ASC, s.id ASC
                       LIMIT 1""",
            due_params=(self.account_id, now_text),
            scope_error="Join slot escaped its account scheduler scope",
            database_error="Failed to queue account-scoped join slot",
        )


class AccountCampaignContext(
    CommentCampaignAPIMixin,
    JoinCampaignAPIMixin,
):
    """Run existing campaign scheduling methods against one account view."""

    def __init__(self, root, account_id: int) -> None:
        self.root = root
        self.account_id = int(account_id)
        self.database = AccountCampaignDatabaseView(root.database, self.account_id)
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
        value = self.root._strict_account_secret(
            self.account_id, "openai.api_key"
        )
        return cast(str | None, value)

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
    accounts = list(root.database.list_telegram_accounts())
    if not accounts:
        legacy_account_id = int(
            root.database.get_setting("telegram.account_id", 0) or 0
        )
        if legacy_account_id > 0:
            accounts = [{
                "telegram_account_id": legacy_account_id,
                "runtime_state": "active",
                "stopped": False,
            }]
    for account in accounts:
        account_id = int(account.get("telegram_account_id") or 0)
        if account_id <= 0:
            continue
        state = str(account.get("runtime_state") or "")
        if bool(account.get("stopped")) or state in {
            "stopping",
            "stopped",
            "authorization_required",
            "restricted",
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
