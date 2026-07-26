from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_comment_delivery_context_v21(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Scope comment deliveries by campaign, action and discussion context.

    Some supported legacy databases contain an early, minimal
    ``comment_deliveries`` table without source columns. Such rows cannot be
    associated with a Telegram target and are therefore not copied; all richer
    v16-v20 rows are preserved with campaign ``0`` and action ``comment``.
    """

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            """CREATE TABLE comment_deliveries_v21(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   account_id INTEGER NOT NULL DEFAULT 0,
                   campaign_id INTEGER NOT NULL DEFAULT 0,
                   action_type TEXT NOT NULL DEFAULT 'comment',
                   channel_id INTEGER NOT NULL,
                   post_id INTEGER NOT NULL,
                   linked_chat_id INTEGER NOT NULL DEFAULT 0,
                   comment_message_id INTEGER,
                   text TEXT,
                   status TEXT NOT NULL DEFAULT 'sending',
                   error TEXT,
                   reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(
                       account_id,
                       campaign_id,
                       action_type,
                       channel_id,
                       post_id,
                       linked_chat_id
                   )
               )"""
        )

        if _table_exists(conn, "comment_deliveries"):
            columns = _column_names(conn, "comment_deliveries")
            if {"channel_id", "post_id"}.issubset(columns):

                def source(name: str, fallback: str) -> str:
                    return name if name in columns else fallback

                conn.execute(
                    f"""INSERT OR IGNORE INTO comment_deliveries_v21(
                            id, account_id, campaign_id, action_type,
                            channel_id, post_id, linked_chat_id,
                            comment_message_id, text, status, error,
                            reserved_at, updated_at
                        )
                        SELECT
                            {source("id", "NULL")},
                            {source("account_id", "0")},
                            {source("campaign_id", "0")},
                            {source("action_type", "'comment'")},
                            channel_id,
                            post_id,
                            COALESCE({source("linked_chat_id", "0")}, 0),
                            {source("comment_message_id", "NULL")},
                            {source("text", "NULL")},
                            COALESCE({source("status", "'sending'")}, 'sending'),
                            {source("error", "NULL")},
                            COALESCE({source("reserved_at", "CURRENT_TIMESTAMP")}, CURRENT_TIMESTAMP),
                            COALESCE({source("updated_at", "CURRENT_TIMESTAMP")}, CURRENT_TIMESTAMP)
                        FROM comment_deliveries"""
                )
            conn.execute("DROP TABLE comment_deliveries")

        conn.execute("ALTER TABLE comment_deliveries_v21 RENAME TO comment_deliveries")
        conn.execute(
            """CREATE INDEX idx_delivery_lookup
               ON comment_deliveries(
                   account_id, campaign_id, action_type,
                   channel_id, post_id, linked_chat_id, status
               )"""
        )
        conn.execute(
            """CREATE INDEX idx_delivery_recovery
               ON comment_deliveries(status, reserved_at)"""
        )
        conn.execute(
            """CREATE INDEX idx_delivery_campaign
               ON comment_deliveries(account_id, campaign_id, status)"""
        )
        # Preserve the legacy/default API invariant: without an explicit
        # campaign/action, one account may reserve a source post only once.
        conn.execute(
            """CREATE UNIQUE INDEX uq_delivery_default_source
               ON comment_deliveries(account_id, channel_id, post_id)
               WHERE campaign_id=0 AND action_type='comment'"""
        )

        if _table_exists(conn, "account_comment_templates"):
            conn.execute(
                "UPDATE account_comment_templates SET visible_count=10 "
                "WHERE visible_count<>10"
            )
        if _table_exists(conn, "migrations"):
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(21)")
        conn.execute("PRAGMA user_version = 21")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
