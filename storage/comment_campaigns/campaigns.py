from __future__ import annotations

from typing import TYPE_CHECKING

import json
import logging
from datetime import timedelta

from core.config import (
    DEFAULT_MAX_CHANNELS_PER_RUN,
    DEFAULT_POST_JOIN_DELAY_MAX_SECONDS,
)
from core.campaign_schedule import (
    generate_random_slots,
    to_db_time,
    utc_now,
)
from storage.db_account_activity import (
    WARMUP_CAMPAIGN_CONFLICT_MESSAGE,
    get_active_account_activity_lease_in_transaction,
)
from storage.db_common import DatabaseError, json_dumps_safe, resolve_account_id
from storage.comment_campaigns.common import _timedelta_microseconds

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class CommentCampaignLifecycleMixin(_MixinHost):
    """Comment campaign creation, queries and lifecycle transitions."""

    def create_comment_campaign(
        self,
        comments,
        *,
        daily_limit=DEFAULT_MAX_CHANNELS_PER_RUN,
        slot_count=None,
        duration_hours=24,
        continuous=True,
        start_at=None,
        allow_existing=False,
        allow_empty_comments=False,
        rng=None,
        account_id=None,
        comment_settings_snapshot=None,
    ):
        """Create one persistent UTC campaign and its randomized schedule.

        ``daily_limit`` is the user-selected 24-hour cadence. ``slot_count`` is
        the number of currently eligible unique channels. When fewer channels
        are available than the selected cadence, only that many slots are
        created, but their spacing still follows the slider value.
        """
        owner_account_id = resolve_account_id(self, account_id)
        normalized = [
            str(item).strip()
            for item in comments
            if isinstance(item, str) and item.strip()
        ]
        if not normalized and not bool(allow_empty_comments):
            raise DatabaseError("At least one non-empty comment is required")
        limit = max(1, min(1000, int(daily_limit)))
        planned_count = (
            limit if slot_count is None else max(0, min(limit, int(slot_count)))
        )
        if planned_count <= 0:
            raise DatabaseError("At least one campaign slot is required")
        hours = max(1, min(168, int(duration_hours)))
        start = start_at or utc_now()
        start = (
            start
            if getattr(start, "tzinfo", None)
            else start.replace(tzinfo=utc_now().tzinfo)
        )
        end = start + timedelta(hours=hours)
        # The slider controls cadence, while the number of real slots is capped
        # by currently eligible unique channels. Build the full cadence and keep
        # only the first planned slots so, for example, 11 channels at 1000/day
        # finish in roughly 15 minutes instead of being stretched across 24 hours.
        slots = generate_random_slots(
            start,
            end,
            limit,
            rng=rng,
            minimum_gap_seconds=DEFAULT_POST_JOIN_DELAY_MAX_SECONDS,
        )[:planned_count]
        cadence_seconds = _timedelta_microseconds(end - start) / 1_000_000 / limit
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                warmup = get_active_account_activity_lease_in_transaction(
                    conn, owner_account_id
                )
                if warmup is not None:
                    raise DatabaseError(WARMUP_CAMPAIGN_CONFLICT_MESSAGE)
                if not allow_existing:
                    active = conn.execute(
                        """SELECT id FROM comment_campaigns
                           WHERE account_id=?
                             AND status IN ('running','paused','network_wait','cycle_wait')
                           ORDER BY id DESC LIMIT 1""",
                        (owner_account_id,),
                    ).fetchone()
                    if active is not None:
                        raise DatabaseError("An active comment campaign already exists")
                cursor = conn.execute(
                    """INSERT INTO comment_campaigns(
                           account_id, status, started_at, ends_at, daily_limit, cadence_seconds, continuous,
                           comments_json, attempted_count, sent_count, created_at, updated_at)
                       VALUES(?, 'running', ?, ?, ?, ?, ?, ?, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (
                        owner_account_id,
                        to_db_time(start),
                        to_db_time(end),
                        limit,
                        cadence_seconds,
                        1 if continuous else 0,
                        json_dumps_safe(normalized),
                    ),
                )
                campaign_id = int(cursor.lastrowid)
                conn.executemany(
                    """INSERT INTO comment_schedule(
                           campaign_id, slot_index, scheduled_at, status, created_at)
                       VALUES(?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
                    [
                        (campaign_id, index, to_db_time(moment))
                        for index, moment in enumerate(slots, start=1)
                    ],
                )
                snapshot = dict(comment_settings_snapshot or {})
                source = str(snapshot.get("comment_source") or "prepared").strip().lower()
                if source not in {"prepared", "openai"}:
                    raise DatabaseError(
                        f"Unsupported campaign comment source: {source!r}"
                    )
                conn.execute(
                    """INSERT INTO campaign_comment_settings(
                           campaign_id, account_id, comment_source, model,
                           system_prompt, max_words, temperature, timeout_seconds,
                           max_generation_attempts, manual_approval_required, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)""",
                    (
                        campaign_id,
                        owner_account_id,
                        source,
                        snapshot.get("model") if source == "openai" else None,
                        snapshot.get("system_prompt") if source == "openai" else None,
                        snapshot.get("max_words") if source == "openai" else None,
                        snapshot.get("temperature") if source == "openai" else None,
                        snapshot.get("timeout_seconds") if source == "openai" else None,
                        snapshot.get("max_generation_attempts") if source == "openai" else None,
                    ),
                )
            return self.get_comment_campaign(campaign_id)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to create comment campaign: {exc}") from exc

    def get_comment_campaign(self, campaign_id):
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    """SELECT id, account_id, status, started_at, ends_at, daily_limit, cadence_seconds, continuous,
                              comments_json, attempted_count, sent_count, last_comment_text,
                              pause_reason, network_failure_count, network_retry_at,
                              created_at, updated_at
                       FROM comment_campaigns WHERE id=?""",
                    (int(campaign_id),),
                ).fetchone()
                if row is None:
                    return None
                result = dict(row)
                try:
                    result["comments"] = json.loads(result.get("comments_json") or "[]")
                except json.JSONDecodeError:
                    result["comments"] = []
                result["continuous"] = bool(result.get("continuous"))
                return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read comment campaign: {exc}") from exc

    def get_active_comment_campaign(self, account_id=None):
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if owner_account_id <= 0:
                    return None
                row = conn.execute(
                    """SELECT id FROM comment_campaigns
                       WHERE account_id=?
                         AND status IN ('running','paused','network_wait','cycle_wait')
                       ORDER BY id DESC LIMIT 1""",
                    (owner_account_id,),
                ).fetchone()
            return self.get_comment_campaign(row["id"]) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read active campaign: {exc}") from exc

    def get_latest_comment_campaign(self, account_id=None):
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if owner_account_id <= 0:
                    return None
                row = conn.execute(
                    """SELECT id FROM comment_campaigns
                       WHERE account_id=? ORDER BY id DESC LIMIT 1""",
                    (owner_account_id,),
                ).fetchone()
            return self.get_comment_campaign(row["id"]) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read latest campaign: {exc}") from exc

    def get_comment_schedule(self, campaign_id, limit=200):
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT id, campaign_id, slot_index, scheduled_at, status, task_id,
                              channel_id, post_id, linked_chat_id, discussion_message_id,
                              route_cached_at, selected_text, selected_variant_index,
                              executed_at, result, created_at
                       FROM comment_schedule WHERE campaign_id=?
                       ORDER BY slot_index ASC LIMIT ?""",
                    (int(campaign_id), int(limit)),
                ).fetchall()
                return [dict(row) for row in rows]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read campaign schedule: {exc}") from exc

    def get_comment_schedule_summary(self, campaign_id):
        """Return status counts and next slot without loading the entire schedule."""
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    """SELECT status, COUNT(*) AS count
                       FROM comment_schedule WHERE campaign_id=? GROUP BY status""",
                    (int(campaign_id),),
                ).fetchall()
                next_row = conn.execute(
                    """SELECT MIN(scheduled_at) AS next_scheduled_at
                       FROM comment_schedule
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
            raise DatabaseError(
                f"Failed to summarize campaign schedule: {exc}"
            ) from exc

    def pause_comment_campaign(self, campaign_id, reason="Пауза пользователя"):
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE comment_campaigns
                       SET status='paused', pause_reason=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='running'""",
                    (str(reason or "Пауза пользователя"), int(campaign_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to pause campaign: {exc}") from exc

    def resume_comment_campaign(self, campaign_id, *, now=None, rng=None):
        now = now or utc_now()
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                campaign = conn.execute(
                    """SELECT status, continuous FROM comment_campaigns WHERE id=?""",
                    (int(campaign_id),),
                ).fetchone()
                if campaign is None or campaign["status"] != "paused":
                    return False
                cursor = conn.execute(
                    """UPDATE comment_campaigns
                       SET status='running', pause_reason=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='paused'""",
                    (int(campaign_id),),
                )
                if cursor.rowcount != 1:
                    return False
                # The lifecycle transition and every pending-slot move form one
                # SQLite unit of work. If redistribution fails, get_connection()
                # rolls the campaign back to paused instead of leaving a false
                # running state while the worker cancellation scope remains set.
                self._redistribute_pending_comment_slots_in_transaction(
                    conn,
                    campaign_id,
                    now=now,
                    force=True,
                    rng=rng,
                )
                remaining = conn.execute(
                    """SELECT COUNT(*) AS total FROM comment_schedule
                       WHERE campaign_id=?
                         AND status IN ('pending','queued','running')""",
                    (int(campaign_id),),
                ).fetchone()
                if int(remaining["total"] if remaining else 0) == 0:
                    continuous = bool(campaign["continuous"])
                    conn.execute(
                        """UPDATE comment_campaigns
                           SET status=?, pause_reason=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='running'""",
                        (
                            "cycle_wait" if continuous else "completed",
                            (
                                "Все запланированные каналы обработаны; ожидание следующего 24-часового цикла"
                                if continuous
                                else "Все запланированные каналы обработаны"
                            ),
                            int(campaign_id),
                        ),
                    )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to resume campaign: {exc}") from exc

    def stop_comment_campaign(self, campaign_id, reason="Остановлено пользователем"):
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE comment_campaigns
                       SET status='stopped', pause_reason=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','paused','network_wait','cycle_wait')""",
                    (str(reason), int(campaign_id)),
                )
                if cursor.rowcount:
                    conn.execute(
                        """UPDATE comment_schedule
                           SET status='cancelled', result=?, executed_at=CURRENT_TIMESTAMP
                           WHERE campaign_id=? AND status='pending'""",
                        (str(reason), int(campaign_id)),
                    )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to stop campaign: {exc}") from exc

    def complete_comment_campaign(self, campaign_id, reason="Период завершён"):
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """UPDATE comment_campaigns
                       SET status='completed', pause_reason=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('running','paused','network_wait','cycle_wait')""",
                    (str(reason), int(campaign_id)),
                )
                if cursor.rowcount:
                    conn.execute(
                        """UPDATE comment_schedule
                           SET status='missed', result=?, executed_at=CURRENT_TIMESTAMP
                           WHERE campaign_id=? AND status='pending'""",
                        (str(reason), int(campaign_id)),
                    )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to complete campaign: {exc}") from exc
