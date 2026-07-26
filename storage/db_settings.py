from __future__ import annotations

from core.redaction import sanitize_log_text

import json
import logging
import os
import uuid

from storage.db_common import DatabaseError, resolve_account_id

log = logging.getLogger(__name__)

# The GUI activity journal is persisted in SQLite so it survives restarts.
# Keep up to five MiB of user-facing history. File-based technical logs have
# a separate two-MiB rotating budget in core.logging_setup.

_MAINTENANCE_OWNER = f"{os.getpid()}:{uuid.uuid4().hex}"
_MAINTENANCE_CLAIM_LEASE_SECONDS = 15 * 60


def _maintenance_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _parse_maintenance_claim(raw: object) -> dict[str, object]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Releases before v25 stored only the date. Such a claim has no owner
        # that can still prove liveness and is therefore recoverable on restart.
        return {"date": text, "legacy": True}
    return value if isinstance(value, dict) else {}


PERSISTENT_LOG_BUDGET_BYTES = 5 * 1024 * 1024
MAX_PERSISTENT_LOG_ENTRY_BYTES = 64 * 1024
_PERSISTENT_LOG_ROW_OVERHEAD_BYTES = 48
_PERSISTENT_LOG_CREATED_AT_BYTES = 19
_PERSISTENT_LOG_SIZE_SETTING = "internal.logs.retained_bytes"
# After crossing five MiB, prune in one batch to four MiB. The one-MiB
# hysteresis avoids repeatedly deleting one row on every subsequent insert.
_PERSISTENT_LOG_PRUNE_TARGET_BYTES = 4 * 1024 * 1024


def _truncate_utf8(value, max_bytes):
    text = str(value or "")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    marker = "… [обрезано]".encode("utf-8")
    available = max(0, int(max_bytes) - len(marker))
    prefix = encoded[:available]
    while prefix:
        try:
            return prefix.decode("utf-8") + marker.decode("utf-8")
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker.decode("utf-8")


class SettingsRepositoryMixin:
    @staticmethod
    def _persistent_log_entry_bytes(level, message) -> int:
        return (
            len(str(level or "").encode("utf-8", errors="replace"))
            + len(str(message or "").encode("utf-8", errors="replace"))
            + _PERSISTENT_LOG_CREATED_AT_BYTES
            + _PERSISTENT_LOG_ROW_OVERHEAD_BYTES
        )

    @staticmethod
    def _set_persistent_log_retained_bytes(conn, value: int) -> None:
        conn.execute(
            """INSERT INTO settings(key, value, updated_at)
               VALUES(?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,
                   updated_at=CURRENT_TIMESTAMP""",
            (_PERSISTENT_LOG_SIZE_SETTING, str(max(0, int(value)))),
        )

    @classmethod
    def _get_persistent_log_retained_bytes(cls, conn) -> int:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (_PERSISTENT_LOG_SIZE_SETTING,),
        ).fetchone()
        if row is not None:
            try:
                return max(0, int(row[0] or 0))
            except (TypeError, ValueError, OverflowError):
                pass
        total = int(
            conn.execute(
                """SELECT COALESCE(SUM(
                       length(CAST(level AS BLOB))
                     + length(CAST(message AS BLOB))
                     + length(CAST(created_at AS BLOB))
                     + ?), 0)
                   FROM logs""",
                (_PERSISTENT_LOG_ROW_OVERHEAD_BYTES,),
            ).fetchone()[0]
            or 0
        )
        cls._set_persistent_log_retained_bytes(conn, total)
        return max(0, total)

    @classmethod
    def _note_persistent_log_insert(
        cls,
        conn,
        level,
        message,
        *,
        retained_before: int,
    ) -> int:
        updated = max(0, int(retained_before)) + cls._persistent_log_entry_bytes(
            level,
            message,
        )
        cls._set_persistent_log_retained_bytes(conn, updated)
        return updated

    @classmethod
    def _prune_persistent_logs_to_budget(cls, conn, *, force: bool = False):
        retained = cls._get_persistent_log_retained_bytes(conn)
        if not force and retained <= PERSISTENT_LOG_BUDGET_BYTES:
            return 0

        rows = conn.execute(
            """SELECT id,
                      length(CAST(level AS BLOB))
                    + length(CAST(message AS BLOB))
                    + length(CAST(created_at AS BLOB))
                    + ? AS retained_bytes
               FROM logs ORDER BY id ASC""",
            (_PERSISTENT_LOG_ROW_OVERHEAD_BYTES,),
        ).fetchall()
        total = sum(max(0, int(row["retained_bytes"] or 0)) for row in rows)
        if total <= PERSISTENT_LOG_BUDGET_BYTES:
            cls._set_persistent_log_retained_bytes(conn, total)
            return 0

        target = min(PERSISTENT_LOG_BUDGET_BYTES, _PERSISTENT_LOG_PRUNE_TARGET_BYTES)
        remove = []
        for row in rows:
            if total <= target:
                break
            remove.append((int(row["id"]),))
            total -= max(0, int(row["retained_bytes"] or 0))
        for offset in range(0, len(remove), 500):
            conn.executemany(
                "DELETE FROM logs WHERE id=?", remove[offset : offset + 500]
            )
        cls._set_persistent_log_retained_bytes(conn, total)
        return len(remove)

    def get_logs(self, level=None, limit=100, account_id=None):
        """Return only activity rows owned by one Telegram account.

        Account 0 is reserved for unauthenticated/legacy activity. It is never
        merged into an authenticated account journal.
        """
        owner_account_id = resolve_account_id(self, account_id)
        try:
            with self.get_connection() as conn:
                if level:
                    cursor = conn.execute(
                        """SELECT id, account_id, level, message, created_at
                           FROM logs
                           WHERE account_id=? AND level=?
                           ORDER BY id DESC LIMIT ?""",
                        (owner_account_id, level, limit),
                    )
                else:
                    cursor = conn.execute(
                        """SELECT id, account_id, level, message, created_at
                           FROM logs
                           WHERE account_id=?
                           ORDER BY id DESC LIMIT ?""",
                        (owner_account_id, limit),
                    )
                return [dict(row) for row in cursor.fetchall()]
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to get logs: {e}") from e

    def insert_log(self, level, message, *, account_id=None):
        """Insert one bounded account-scoped activity entry."""
        owner_account_id = resolve_account_id(self, account_id)
        try:
            normalized_level = _truncate_utf8(str(level or "INFO").upper(), 32)
            normalized_message = _truncate_utf8(
                sanitize_log_text(message), MAX_PERSISTENT_LOG_ENTRY_BYTES
            )
            with self.get_connection() as conn:
                # Serialize the read-modify-write counter with the log insert.
                # Without an early write reservation, two thread-local SQLite
                # connections can read the same retained byte count and one
                # increment is lost, allowing the journal to exceed its cap.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                retained_before = self._get_persistent_log_retained_bytes(conn)
                conn.execute(
                    """INSERT INTO logs(account_id, level, message, created_at)
                       VALUES(?, ?, ?, CURRENT_TIMESTAMP)""",
                    (owner_account_id, normalized_level, normalized_message),
                )
                self._note_persistent_log_insert(
                    conn,
                    normalized_level,
                    normalized_message,
                    retained_before=retained_before,
                )
                self._prune_persistent_logs_to_budget(conn)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to insert log: {exc}") from exc

    def insert_template(self, name, texts):
        """Insert comment template."""
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO comment_templates(name, text_1, text_2, text_3, text_4, text_5, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(name) DO UPDATE SET
                           text_1=excluded.text_1, text_2=excluded.text_2,
                           text_3=excluded.text_3, text_4=excluded.text_4,
                           text_5=excluded.text_5, updated_at=CURRENT_TIMESTAMP""",
                    (
                        name,
                        texts.get("text_1"),
                        texts.get("text_2"),
                        texts.get("text_3"),
                        texts.get("text_4"),
                        texts.get("text_5"),
                    ),
                )
            log.info("Template '%s' saved", name)
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to insert template: {e}") from e

    def get_templates(self):
        """Get all comment templates."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """SELECT id, name, text_1, text_2, text_3, text_4, text_5 FROM comment_templates"""
                )
                return [dict(row) for row in cursor.fetchall()]
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Failed to get templates: {e}") from e

    def set_setting(self, key, value):
        """Store one application setting in SQLite."""
        try:
            encoded = None if value is None else str(value)
            with self.get_connection() as conn:
                conn.execute(
                    """INSERT INTO settings(key, value, updated_at)
                       VALUES(?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value, updated_at=CURRENT_TIMESTAMP
                       WHERE settings.value IS NOT excluded.value""",
                    (str(key), encoded),
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to save setting {key!r}: {exc}") from exc

    def set_settings(self, values):
        """Store multiple application settings atomically."""
        try:
            with self.get_connection() as conn:
                for key, value in dict(values).items():
                    encoded = None if value is None else str(value)
                    conn.execute(
                        """INSERT INTO settings(key, value, updated_at)
                           VALUES(?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET
                               value=excluded.value, updated_at=CURRENT_TIMESTAMP
                           WHERE settings.value IS NOT excluded.value""",
                        (str(key), encoded),
                    )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to save settings: {exc}") from exc

    def get_setting(self, key, default=None):
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (str(key),)
                ).fetchone()
                return row[0] if row is not None else default
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read setting {key!r}: {exc}") from exc

    def get_settings(self, prefix=None):
        try:
            with self.get_connection() as conn:
                if prefix:
                    rows = conn.execute(
                        "SELECT key, value FROM settings WHERE key LIKE ? ORDER BY key",
                        (f"{prefix}%",),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT key, value FROM settings ORDER BY key"
                    ).fetchall()
                return {row["key"]: row["value"] for row in rows}
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to read settings: {exc}") from exc

    def delete_setting(self, key):
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM settings WHERE key=?", (str(key),))
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to delete setting {key!r}: {exc}") from exc

    def prune_old_data(
        self,
        *,
        log_days=30,
        task_days=90,
        history_days=180,
        campaign_days=180,
    ):
        """Apply bounded local retention and return deleted row counts."""
        try:
            log_days = max(1, int(log_days))
            task_days = max(1, int(task_days))
            history_days = max(1, int(history_days))
            campaign_days = max(1, int(campaign_days))
        except (TypeError, ValueError, OverflowError) as exc:
            raise DatabaseError("Retention periods must be positive integers") from exc

        modifiers = {
            "logs": f"-{log_days} days",
            "tasks": f"-{task_days} days",
            "history": f"-{history_days} days",
            "campaigns": f"-{campaign_days} days",
        }
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                deleted = {}
                deleted["logs"] = conn.execute(
                    "DELETE FROM logs WHERE created_at < datetime('now', ?)",
                    (modifiers["logs"],),
                ).rowcount
                deleted["logs_budget"] = self._prune_persistent_logs_to_budget(
                    conn,
                    force=True,
                )
                deleted["comment_history"] = conn.execute(
                    "DELETE FROM comment_history WHERE sent_at < datetime('now', ?)",
                    (modifiers["history"],),
                ).rowcount
                deleted["join_events"] = conn.execute(
                    "DELETE FROM join_events WHERE joined_at < datetime('now', ?)",
                    (modifiers["history"],),
                ).rowcount
                deleted["tasks"] = conn.execute(
                    """DELETE FROM tasks
                       WHERE status IN ('completed','cancelled')
                         AND updated_at < datetime('now', ?)
                         AND NOT EXISTS(
                             SELECT 1 FROM comment_schedule s
                             WHERE s.task_id=tasks.id
                               AND s.status IN ('queued','running')
                         )
                         AND NOT EXISTS(
                             SELECT 1 FROM join_schedule s
                             WHERE s.task_id=tasks.id
                               AND s.status IN ('queued','running')
                         )""",
                    (modifiers["tasks"],),
                ).rowcount
                deleted["failed_tasks"] = conn.execute(
                    """DELETE FROM tasks
                       WHERE status='failed'
                         AND updated_at < datetime('now', ?)
                         AND NOT EXISTS(
                             SELECT 1 FROM comment_schedule s
                             WHERE s.task_id=tasks.id
                               AND s.status IN ('queued','running')
                         )
                         AND NOT EXISTS(
                             SELECT 1 FROM join_schedule s
                             WHERE s.task_id=tasks.id
                               AND s.status IN ('queued','running')
                         )""",
                    (modifiers["history"],),
                ).rowcount
                deleted["direct_message_deliveries"] = conn.execute(
                    """DELETE FROM direct_message_deliveries
                       WHERE status IN ('sent','failed')
                         AND updated_at < datetime('now', ?)""",
                    (modifiers["history"],),
                ).rowcount
                deleted["comment_campaigns"] = conn.execute(
                    """DELETE FROM comment_campaigns
                       WHERE status IN ('completed','stopped')
                         AND updated_at < datetime('now', ?)""",
                    (modifiers["campaigns"],),
                ).rowcount
                deleted["join_campaigns"] = conn.execute(
                    """DELETE FROM join_campaigns
                       WHERE status IN ('completed','stopped')
                         AND updated_at < datetime('now', ?)""",
                    (modifiers["campaigns"],),
                ).rowcount
            with self.get_connection() as conn:
                conn.execute("PRAGMA optimize")
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return deleted
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to prune old data: {exc}") from exc

    def run_daily_maintenance(self):
        """Run retention once per UTC day with a crash-recoverable owner lease."""
        claim_key = "maintenance.prune_claim_date"
        last_key = "maintenance.last_prune_date"
        today = None
        claim_value = None
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                clock = conn.execute(
                    "SELECT date('now'), CAST(strftime('%s','now') AS INTEGER)"
                ).fetchone()
                today = str(clock[0])
                now_epoch = int(clock[1])
                last_row = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (last_key,)
                ).fetchone()
                claim_row = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (claim_key,)
                ).fetchone()
                if last_row is not None and str(last_row[0] or "") == today:
                    return None

                current_claim = _parse_maintenance_claim(
                    claim_row[0] if claim_row is not None else None
                )
                if str(current_claim.get("date") or "") == today:
                    owner = str(current_claim.get("owner") or "")
                    try:
                        owner_pid = int(str(current_claim.get("pid") or "0"))
                    except (TypeError, ValueError, OverflowError):
                        owner_pid = 0
                    try:
                        expires_at = int(str(current_claim.get("expires_at") or "0"))
                    except (TypeError, ValueError, OverflowError):
                        expires_at = 0
                    if owner == _MAINTENANCE_OWNER:
                        return None
                    # Preserve a live concurrent owner, but immediately recover a
                    # legacy/dead-process claim left by a crash. The bounded lease
                    # handles platforms where process liveness cannot be proven.
                    if (
                        not current_claim.get("legacy")
                        and expires_at > now_epoch
                        and _maintenance_process_alive(owner_pid)
                    ):
                        return None

                claim = {
                    "date": today,
                    "owner": _MAINTENANCE_OWNER,
                    "pid": os.getpid(),
                    "claimed_at": now_epoch,
                    "expires_at": now_epoch + _MAINTENANCE_CLAIM_LEASE_SECONDS,
                }
                claim_value = json.dumps(
                    claim, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                conn.execute(
                    """INSERT INTO settings(key, value, updated_at)
                       VALUES(?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                    (claim_key, claim_value),
                )

            try:
                result = self.prune_old_data()
            except Exception:
                with self.get_connection() as conn:
                    conn.execute(
                        "DELETE FROM settings WHERE key=? AND value=?",
                        (claim_key, claim_value),
                    )
                raise

            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                owned = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (claim_key,)
                ).fetchone()
                if owned is None or str(owned[0] or "") != claim_value:
                    # Another live owner replaced the lease. Do not publish its
                    # completion marker or delete its claim.
                    return result
                conn.execute(
                    """INSERT INTO settings(key, value, updated_at)
                       VALUES(?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                    (last_key, today),
                )
                conn.execute(
                    "DELETE FROM settings WHERE key=? AND value=?",
                    (claim_key, claim_value),
                )
            return result
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Daily maintenance failed: {exc}") from exc
