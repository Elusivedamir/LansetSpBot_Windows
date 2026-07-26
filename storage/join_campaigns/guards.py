from __future__ import annotations

from typing import TYPE_CHECKING

import logging
import math
from datetime import timedelta

from core.campaign_schedule import (
    from_db_time,
    to_db_time,
    to_db_time_precise,
    utc_now,
)
from storage.db_common import DatabaseError

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class JoinGuardRepositoryMixin(_MixinHost):
    """Rolling-window join guards and event history."""

    def get_join_guard(
        self,
        *,
        max_joins,
        min_interval_seconds,
        now=None,
        window_seconds=86400,
        account_id=None,
    ):
        now = now or utc_now()
        window_start = now - timedelta(seconds=max(1, int(window_seconds)))
        try:
            with self.get_connection() as conn:
                if account_id is None:
                    row = conn.execute(
                        """SELECT COALESCE(SUM(
                                      CASE WHEN event_kind='joined' THEN 1 ELSE 0 END
                                  ), 0) AS joined_count,
                                  COUNT(*) AS effective_count,
                                  MAX(event_at) AS last_joined_at,
                                  MIN(event_at) AS oldest_joined_at
                           FROM (
                               SELECT joined_at AS event_at, 'joined' AS event_kind
                               FROM join_events
                               WHERE result='joined' AND joined_at>=?
                               UNION ALL
                               SELECT updated_at AS event_at, 'uncertain' AS event_kind
                               FROM saved_dialog_memberships
                               WHERE status='uncertain' AND updated_at>=?
                           )""",
                        (to_db_time(window_start), to_db_time(window_start)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """SELECT COALESCE(SUM(
                                      CASE WHEN event_kind='joined' THEN 1 ELSE 0 END
                                  ), 0) AS joined_count,
                                  COUNT(*) AS effective_count,
                                  MAX(event_at) AS last_joined_at,
                                  MIN(event_at) AS oldest_joined_at
                           FROM (
                               SELECT joined_at AS event_at, 'joined' AS event_kind
                               FROM join_events
                               WHERE result='joined' AND joined_at>=?
                                 AND account_id=?
                               UNION ALL
                               SELECT updated_at AS event_at, 'uncertain' AS event_kind
                               FROM saved_dialog_memberships
                               WHERE status='uncertain' AND updated_at>=?
                                 AND account_id=?
                           )""",
                        (
                            to_db_time(window_start),
                            int(account_id),
                            to_db_time(window_start),
                            int(account_id),
                        ),
                    ).fetchone()
            joined_count = int(row["joined_count"] or 0)
            count = int(row["effective_count"] or 0)
            last = from_db_time(row["last_joined_at"])
            wait_seconds = 0
            interval_allowed = True
            if last is not None:
                elapsed = max(0.0, (now - last).total_seconds())
                interval_remaining = float(min_interval_seconds) - elapsed
                interval_allowed = interval_remaining <= 0
                wait_seconds = max(0, math.ceil(interval_remaining))
            oldest = from_db_time(row["oldest_joined_at"])
            if count >= int(max_joins) and oldest is not None:
                window_wait = max(
                    0,
                    math.ceil(
                        (
                            oldest
                            + timedelta(seconds=max(1, int(window_seconds)))
                            - now
                        ).total_seconds()
                    ),
                )
                wait_seconds = max(wait_seconds, window_wait)
            return {
                "allowed": count < int(max_joins) and interval_allowed,
                "joined_count": joined_count,
                "uncertain_count": max(0, count - joined_count),
                "effective_count": count,
                "remaining": max(0, int(max_joins) - count),
                "wait_seconds": wait_seconds,
                "last_joined_at": to_db_time_precise(last) if last else None,
            }
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to check join guard: {exc}") from exc

    def record_join_event(
        self,
        linked_chat_id,
        result="joined",
        *,
        campaign_id=None,
        saved_dialog_id=None,
        account_id=None,
    ):
        try:
            with self.get_connection() as conn:
                resolved_account_id = account_id
                if resolved_account_id is None:
                    row = conn.execute(
                        "SELECT value FROM settings WHERE key='telegram.account_id'"
                    ).fetchone()
                    try:
                        candidate = int(row["value"]) if row is not None else 0
                    except (TypeError, ValueError, OverflowError):
                        candidate = 0
                    resolved_account_id = candidate if candidate > 0 else None
                conn.execute(
                    """INSERT INTO join_events(linked_chat_id, joined_at, result, campaign_id, saved_dialog_id, account_id)
                       VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        int(linked_chat_id or 0),
                        to_db_time_precise(utc_now()),
                        str(result),
                        campaign_id,
                        saved_dialog_id,
                        resolved_account_id,
                    ),
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to record join event: {exc}") from exc
