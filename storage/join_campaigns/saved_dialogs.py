from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from core.campaign_schedule import to_db_time_precise, utc_now
from storage.db_common import DatabaseError

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class SavedDialogRepositoryMixin(_MixinHost):
    """Saved Telegram dialogs and membership state."""

    @staticmethod
    def _upsert_saved_dialog_on_connection(conn, data, *, account_id, phone=None):
        peer_raw = data.get("peer_id")
        peer_id = int(peer_raw) if peer_raw is not None else None
        username = str(data.get("username") or "").lstrip("@").strip() or None
        title = str(data.get("title") or "").strip() or "Без названия"
        kind = str(data.get("kind") or "channel")
        invite_link = str(data.get("invite_link") or "").strip() or None

        row = None
        if peer_id is not None:
            row = conn.execute(
                "SELECT id, peer_id FROM saved_dialogs WHERE peer_id=?", (peer_id,)
            ).fetchone()

        username_owner = None
        if username:
            username_owner = conn.execute(
                "SELECT id, peer_id FROM saved_dialogs WHERE lower(username)=lower(?)",
                (username,),
            ).fetchone()

        if row is None and username_owner is not None:
            owner_peer = username_owner["peer_id"]
            if peer_id is None or owner_peer is None or int(owner_peer) == peer_id:
                # Invite-only rows can be upgraded once Telegram exposes a peer id.
                row = username_owner
            else:
                # Telegram usernames are mutable. Never move memberships/history
                # from the previous peer to the new owner.
                conn.execute(
                    "UPDATE saved_dialogs SET username=NULL WHERE id=?",
                    (int(username_owner["id"]),),
                )
        elif row is not None and username_owner is not None:
            if int(username_owner["id"]) != int(row["id"]):
                conn.execute(
                    "UPDATE saved_dialogs SET username=NULL WHERE id=?",
                    (int(username_owner["id"]),),
                )

        if row is None:
            cursor = conn.execute(
                """INSERT INTO saved_dialogs(peer_id, username, title, kind, invite_link,
                       source_account_id, source_phone, saved_at, last_seen_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (
                    peer_id,
                    username,
                    title,
                    kind,
                    invite_link,
                    int(account_id),
                    phone,
                ),
            )
            dialog_id = int(cursor.lastrowid)
        else:
            dialog_id = int(row["id"])
            conn.execute(
                """UPDATE saved_dialogs
                   SET peer_id=COALESCE(?, peer_id),
                       username=?, title=?, kind=?,
                       invite_link=COALESCE(?, invite_link),
                       source_account_id=?, source_phone=?,
                       last_seen_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    peer_id,
                    username,
                    title,
                    kind,
                    invite_link,
                    int(account_id),
                    phone,
                    dialog_id,
                ),
            )
        previous_membership = conn.execute(
            """SELECT status FROM saved_dialog_memberships
               WHERE saved_dialog_id=? AND account_id=?""",
            (dialog_id, int(account_id)),
        ).fetchone()
        conn.execute(
            """INSERT INTO saved_dialog_memberships(saved_dialog_id, account_id, status, last_error, updated_at)
               VALUES(?, ?, 'member', NULL, CURRENT_TIMESTAMP)
               ON CONFLICT(saved_dialog_id, account_id) DO UPDATE SET
                   status='member', last_error=NULL, updated_at=CURRENT_TIMESTAMP""",
            (dialog_id, int(account_id)),
        )
        if (
            previous_membership is not None
            and previous_membership["status"] == "uncertain"
        ):
            resolved_peer_id = peer_id
            if resolved_peer_id is None:
                resolved = conn.execute(
                    "SELECT peer_id FROM saved_dialogs WHERE id=?", (dialog_id,)
                ).fetchone()
                resolved_peer_id = resolved["peer_id"] if resolved else 0
            conn.execute(
                """INSERT INTO join_events(
                       linked_chat_id, joined_at, result, saved_dialog_id, account_id)
                   VALUES(?, ?, 'joined', ?, ?)""",
                (
                    int(resolved_peer_id or 0),
                    to_db_time_precise(utc_now()),
                    dialog_id,
                    int(account_id),
                ),
            )
        return dialog_id

    def upsert_saved_dialogs_batch(self, rows, *, account_id, phone=None):
        """Persist a bounded dialog batch without one transaction per row."""
        normalized = list(rows)
        if not normalized:
            return []
        try:
            with self.get_connection() as conn:
                return [
                    self._upsert_saved_dialog_on_connection(
                        conn, row, account_id=account_id, phone=phone
                    )
                    for row in normalized
                ]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to save dialog batch: {exc}") from exc

    def upsert_saved_dialog(self, data, *, account_id, phone=None):
        ids = self.upsert_saved_dialogs_batch(
            [data], account_id=account_id, phone=phone
        )
        return ids[0]

    def mark_unseen_saved_dialogs_left(self, *, account_id, seen_dialog_ids):
        """Mark dialogs absent from a completed Telegram sync as no longer joined."""
        try:
            normalized = sorted({int(value) for value in seen_dialog_ids})
            with self.get_connection() as conn:
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS seen_saved_dialog_ids(id INTEGER PRIMARY KEY)"
                )
                conn.execute("DELETE FROM seen_saved_dialog_ids")
                if normalized:
                    conn.executemany(
                        "INSERT INTO seen_saved_dialog_ids(id) VALUES(?)",
                        ((value,) for value in normalized),
                    )
                cursor = conn.execute(
                    """UPDATE saved_dialog_memberships
                       SET status='left', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE account_id=? AND status IN ('member','uncertain')
                         AND NOT EXISTS(
                             SELECT 1 FROM seen_saved_dialog_ids seen
                             WHERE seen.id=saved_dialog_memberships.saved_dialog_id
                         )""",
                    (int(account_id),),
                )
                return cursor.rowcount
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to finalize saved-dialog sync: {exc}") from exc

    def get_saved_dialogs(self, account_id=None):
        try:
            with self.get_connection() as conn:
                if account_id is None:
                    rows = conn.execute(
                        """SELECT d.*, NULL AS membership_status FROM saved_dialogs d
                           ORDER BY lower(d.title)"""
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT d.*, m.status AS membership_status, m.last_error AS membership_error
                           FROM saved_dialog_memberships m
                           JOIN saved_dialogs d ON d.id=m.saved_dialog_id
                           WHERE m.account_id=?
                           ORDER BY lower(d.title)""",
                        (int(account_id),),
                    ).fetchall()
                return [dict(row) for row in rows]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read saved dialogs: {exc}") from exc

    def set_saved_dialog_membership(
        self, saved_dialog_id, account_id, status, error=None
    ):
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO saved_dialog_memberships(saved_dialog_id, account_id, status, last_error, updated_at)
                       VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(saved_dialog_id, account_id) DO UPDATE SET
                           status=excluded.status, last_error=excluded.last_error, updated_at=CURRENT_TIMESTAMP""",
                    (
                        int(saved_dialog_id),
                        int(account_id),
                        str(status),
                        None if error is None else str(error),
                    ),
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to update saved membership: {exc}") from exc

    def set_peer_membership_uncertain(self, peer_id, account_id, error, *, title=None):
        """Persist an ambiguous join even when no saved-dialog row existed yet.

        Comment campaigns can join a linked discussion directly from the channels
        table. If shutdown interrupts that mutating request after dispatch, create
        or reuse the durable saved-dialog identity so the rolling join guard and a
        later full dialog sync can resolve the unknown result safely.
        """

        try:
            resolved_peer_id = int(peer_id)
            resolved_account_id = int(account_id)
            if resolved_peer_id == 0 or resolved_account_id <= 0:
                raise DatabaseError("Invalid peer/account for uncertain membership")
            resolved_title = (
                str(title).strip() if title is not None else ""
            ) or f"Telegram chat {resolved_peer_id}"
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT id FROM saved_dialogs WHERE peer_id=?",
                    (resolved_peer_id,),
                ).fetchone()
                if row is None:
                    cursor = conn.execute(
                        """INSERT INTO saved_dialogs(
                               peer_id, title, kind, source_account_id,
                               saved_at, last_seen_at)
                           VALUES(?, ?, 'group', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                        (resolved_peer_id, resolved_title, resolved_account_id),
                    )
                    saved_dialog_id = int(cursor.lastrowid)
                else:
                    saved_dialog_id = int(row["id"])
                    conn.execute(
                        """UPDATE saved_dialogs
                           SET title=CASE
                                   WHEN title IS NULL OR trim(title)='' THEN ?
                                   ELSE title
                               END,
                               source_account_id=COALESCE(source_account_id, ?),
                               last_seen_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (resolved_title, resolved_account_id, saved_dialog_id),
                    )
                conn.execute(
                    """INSERT INTO saved_dialog_memberships(
                           saved_dialog_id, account_id, status, last_error, updated_at)
                       VALUES(?, ?, 'uncertain', ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(saved_dialog_id, account_id) DO UPDATE SET
                           status='uncertain', last_error=excluded.last_error,
                           updated_at=CURRENT_TIMESTAMP""",
                    (saved_dialog_id, resolved_account_id, str(error)),
                )
                return saved_dialog_id
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to preserve uncertain peer membership: {exc}"
            ) from exc
