from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_local_channel_ban_v27(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Persist account-scoped permanent exclusions for ambiguous Join results."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "channels" in tables:
            columns = _column_names(conn, "channels")
            additions = {
                "local_ban_reason": "TEXT",
                "local_ban_peer_id": "INTEGER",
                "local_banned_at": "DATETIME",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE channels ADD COLUMN {name} {declaration}"
                    )

            # Preserve the user's already-seen ambiguous Join target when an old
            # v26 database is opened by the fixed build. The old handler stored
            # the English transport message in link_status and then aborted the
            # batch before link_checked_at could be advanced.
            conn.execute(
                """UPDATE channels
                   SET linked_chat_id=NULL,
                       linked_chat_title=NULL,
                       link_status='Заблокирован · результат вступления неизвестен',
                       link_checked_at=COALESCE(link_checked_at, CURRENT_TIMESTAMP),
                       local_ban_reason=COALESCE(
                           local_ban_reason,
                           'Результат вступления неизвестен'
                       ),
                       local_banned_at=COALESCE(local_banned_at, CURRENT_TIMESTAMP),
                       last_sync_at=CURRENT_TIMESTAMP
                   WHERE local_banned_at IS NULL
                     AND (
                         LOWER(COALESCE(link_status, '')) LIKE '%join%result%unknown%'
                         OR LOWER(COALESCE(link_status, '')) LIKE '%delivery result is unknown%'
                         OR LOWER(COALESCE(link_status, '')) LIKE '%результат вступления неизвестен%'
                     )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_channels_local_ban
                   ON channels(account_id, local_banned_at, link_checked_at, target_kind, id)"""
            )

        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        if migrations is not None:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(27)")
        conn.execute("PRAGMA user_version = 27")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
