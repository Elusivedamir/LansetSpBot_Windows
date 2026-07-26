from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from storage.sqlcipher_driver import dbapi as sqlite3
from datetime import timedelta

from core.campaign_schedule import (
    generate_join_slots,
    to_db_time,
    utc_now,
)
from storage.db_common import DatabaseError, resolve_account_id

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class JoinCampaignLifecycleMixin(_MixinHost):
    """Join campaign creation and query operations."""

    def create_join_campaign(
        self, account_id, *, max_per_hour=40, start_at=None, rng=None
    ):
        account_id = int(account_id)
        limit = max(1, int(max_per_hour))
        start = start_at or utc_now()
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """SELECT id FROM join_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait') LIMIT 1""",
                    (account_id,),
                ).fetchone()
                if active:
                    raise DatabaseError("Кампания вступлений уже запущена")
                candidates = conn.execute(
                    """SELECT d.*,
                              m.status AS membership_status,
                              m.last_error AS membership_error
                       FROM saved_dialogs d
                       LEFT JOIN saved_dialog_memberships m
                         ON m.saved_dialog_id=d.id AND m.account_id=?
                       WHERE COALESCE(m.status, '') NOT IN ('member','uncertain')
                         AND NOT EXISTS(
                             SELECT 1 FROM local_ban_targets b
                             WHERE b.account_id=? AND b.peer_id=d.peer_id
                         )
                         AND (d.username IS NOT NULL OR d.invite_link IS NOT NULL)
                       ORDER BY lower(d.title), d.id""",
                    (account_id, account_id),
                ).fetchall()
                if not candidates:
                    raise DatabaseError(
                        "Нет сохранённых публичных каналов/групп для вступления"
                    )
                total = len(candidates)
                moments = generate_join_slots(start, total, rng=rng, max_per_hour=limit)
                end = (
                    (moments[-1] + timedelta(minutes=5))
                    if moments
                    else (start + timedelta(hours=1))
                )
                try:
                    cursor = conn.execute(
                        """INSERT INTO join_campaigns(account_id, status, started_at, ends_at,
                               max_per_hour, total_count, created_at, updated_at)
                           VALUES(?, 'running', ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                        (account_id, to_db_time(start), to_db_time(end), limit, total),
                    )
                except sqlite3.IntegrityError as exc:
                    if "uq_join_campaign_active" in str(exc) or "UNIQUE" in str(exc):
                        raise DatabaseError("Кампания вступлений уже запущена") from exc
                    raise
                campaign_id = int(cursor.lastrowid)
                conn.executemany(
                    """INSERT INTO join_schedule(campaign_id, slot_index, scheduled_at, status,
                           saved_dialog_id, created_at)
                       VALUES(?, ?, ?, 'pending', ?, CURRENT_TIMESTAMP)""",
                    [
                        (campaign_id, idx, to_db_time(moment), int(dialog["id"]))
                        for idx, (moment, dialog) in enumerate(
                            zip(moments, candidates), start=1
                        )
                    ],
                )
            return self.get_join_campaign(campaign_id)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to create join campaign: {exc}") from exc

    def get_join_campaign(self, campaign_id):
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM join_campaigns WHERE id=?", (int(campaign_id),)
                ).fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read join campaign: {exc}") from exc

    def get_active_join_campaign(self, account_id=None):
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if owner_account_id <= 0:
                    return None
                row = conn.execute(
                    """SELECT id FROM join_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait')
                       ORDER BY id DESC LIMIT 1""",
                    (owner_account_id,),
                ).fetchone()
            return self.get_join_campaign(row["id"]) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read active join campaign: {exc}") from exc

    def get_latest_join_campaign(self, account_id=None):
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if owner_account_id <= 0:
                    return None
                row = conn.execute(
                    """SELECT id FROM join_campaigns
                       WHERE account_id=? ORDER BY id DESC LIMIT 1""",
                    (owner_account_id,),
                ).fetchone()
            return self.get_join_campaign(row["id"]) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read latest join campaign: {exc}") from exc

    def get_join_schedule(self, campaign_id, limit=2000):
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT s.*, d.title, d.username, d.kind FROM join_schedule s
                       JOIN saved_dialogs d ON d.id=s.saved_dialog_id
                       WHERE s.campaign_id=? ORDER BY s.slot_index LIMIT ?""",
                    (int(campaign_id), int(limit)),
                ).fetchall()
                return [dict(row) for row in rows]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read join schedule: {exc}") from exc

    def get_join_schedule_summary(self, campaign_id):
        """Return status counts and next slot without materializing thousands of rows."""
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT status, COUNT(*) AS count
                       FROM join_schedule WHERE campaign_id=? GROUP BY status""",
                    (int(campaign_id),),
                ).fetchall()
                next_row = conn.execute(
                    """SELECT MIN(scheduled_at) AS next_scheduled_at
                       FROM join_schedule
                       WHERE campaign_id=? AND status='pending'""",
                    (int(campaign_id),),
                ).fetchone()
            counts = {str(row["status"]): int(row["count"]) for row in rows}
            return {
                "counts": counts,
                "next_scheduled_at": next_row["next_scheduled_at"]
                if next_row
                else None,
                "open_count": sum(
                    counts.get(status, 0) for status in ("pending", "queued", "running")
                ),
            }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to summarize join schedule: {exc}") from exc
