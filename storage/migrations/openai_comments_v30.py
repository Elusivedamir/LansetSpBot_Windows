from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3


def _execute_script(conn, script: str) -> None:
    """Execute statements without sqlite3.executescript's implicit COMMIT."""

    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("Incomplete migration SQL statement")


def migrate_openai_comments_v30(
    path: str | Path,
    *,
    sqlite_timeout_seconds: float = 30.0,
    busy_timeout_ms: int = 30_000,
) -> None:
    """Add OpenAI campaign snapshots and generated draft lifecycle tables."""

    conn = sqlite3.connect(str(path), timeout=float(sqlite_timeout_seconds))
    try:
        conn.execute(f"PRAGMA busy_timeout = {max(100, int(busy_timeout_ms))}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        _execute_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS campaign_comment_settings(
                campaign_id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL,
                comment_source TEXT NOT NULL DEFAULT 'prepared'
                    CHECK(comment_source IN ('prepared','openai')),
                model TEXT,
                system_prompt TEXT,
                max_words INTEGER,
                temperature REAL,
                timeout_seconds REAL,
                max_generation_attempts INTEGER,
                manual_approval_required INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(campaign_id) REFERENCES comment_campaigns(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_campaign_comment_settings_account
                ON campaign_comment_settings(account_id, campaign_id);

            CREATE TABLE IF NOT EXISTS generated_comment_drafts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                campaign_id INTEGER,
                source_channel_id INTEGER NOT NULL,
                source_post_id INTEGER NOT NULL,
                linked_chat_id INTEGER,
                discussion_message_id INTEGER,
                post_text TEXT NOT NULL DEFAULT '',
                post_text_hash TEXT,
                generated_text TEXT,
                edited_text TEXT,
                status TEXT NOT NULL DEFAULT 'generated',
                model TEXT,
                word_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at DATETIME,
                UNIQUE(account_id, source_channel_id, source_post_id),
                FOREIGN KEY(campaign_id) REFERENCES comment_campaigns(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_generated_drafts_account_status
                ON generated_comment_drafts(account_id, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_generated_drafts_campaign
                ON generated_comment_drafts(campaign_id, updated_at DESC);
            """,
        )
        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        if migrations is not None:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(30)")
        conn.execute("PRAGMA user_version = 30")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
