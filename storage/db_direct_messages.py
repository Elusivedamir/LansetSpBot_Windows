from __future__ import annotations

from typing import TYPE_CHECKING

from contextlib import AbstractContextManager

from core.redaction import sanitize_text
from storage.db_common import DatabaseError

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``sqlite3`` is bound to the SQLCipher DBAPI proxy object, not to a
    # module, so its DBAPI classes are imported from the standard library
    # for annotations. The two drivers are DBAPI-compatible.
    from sqlite3 import Connection as SQLiteConnection



class DirectMessageRepositoryMixin:
    """Durable idempotency ledger for non-repeatable direct messages."""

    def get_connection(self) -> AbstractContextManager[SQLiteConnection]:
        """Provided by the concrete Database facade."""
        raise NotImplementedError

    def reserve_direct_message_delivery(self, task_id, chat_id, text):
        try:
            task_id = int(task_id)
            if task_id <= 0:
                raise DatabaseError(
                    "Direct-message delivery requires a positive task id"
                )
            normalized_chat = str(chat_id).strip()
            normalized_text = str(text).strip()
            if not normalized_chat or not normalized_text:
                raise DatabaseError("Direct-message delivery requires chat_id and text")
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """SELECT 1 FROM direct_message_deliveries
                       WHERE chat_id=? AND status IN ('sending','uncertain')
                       LIMIT 1""",
                    (normalized_chat,),
                ).fetchone()
                if active is not None:
                    return False
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO direct_message_deliveries(
                           task_id, chat_id, text, status, reserved_at, updated_at)
                       VALUES(?, ?, ?, 'sending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (task_id, normalized_chat, normalized_text),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to reserve direct-message delivery: {exc}"
            ) from exc

    def get_direct_message_delivery(self, task_id):
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM direct_message_deliveries WHERE task_id=?",
                    (int(task_id),),
                ).fetchone()
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to read direct-message delivery: {exc}"
            ) from exc

    def release_direct_message_delivery(self, task_id):
        """Release only a request proven not to have executed at Telegram."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM direct_message_deliveries WHERE task_id=? AND status='sending'",
                    (int(task_id),),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to release direct-message delivery: {exc}"
            ) from exc

    def mark_direct_message_delivery_uncertain(self, task_id, error):
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE direct_message_deliveries
                       SET status='uncertain', error=?, updated_at=CURRENT_TIMESTAMP
                       WHERE task_id=? AND status IN ('sending','uncertain')""",
                    (sanitize_text(error), int(task_id)),
                )
                return cursor.rowcount == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to mark direct-message delivery uncertain: {exc}"
            ) from exc

    def finalize_direct_message_delivery(self, task_id, *, message_id=None):
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """UPDATE direct_message_deliveries
                       SET status='sent', message_id=?, error=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE task_id=? AND status IN ('sending','uncertain')""",
                    (message_id, int(task_id)),
                )
                if cursor.rowcount != 1:
                    row = conn.execute(
                        "SELECT status FROM direct_message_deliveries WHERE task_id=?",
                        (int(task_id),),
                    ).fetchone()
                    if row is None or str(row["status"]) != "sent":
                        raise DatabaseError(
                            "Direct-message delivery receipt is missing or invalid"
                        )
                return True
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to finalize direct-message delivery: {exc}"
            ) from exc
