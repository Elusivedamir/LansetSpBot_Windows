from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path

from core.redaction import sanitize_json, sanitize_text


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _sanitize_text_column(
    conn: sqlite3.Connection, table: str, id_column: str, value_column: str
) -> None:
    columns = _column_names(conn, table)
    if id_column not in columns or value_column not in columns:
        return
    rows = conn.execute(
        f"SELECT {id_column}, {value_column} FROM {table} "
        f"WHERE {value_column} IS NOT NULL AND {value_column}<>''"
    ).fetchall()
    updates = []
    for identity, value in rows:
        raw = str(value)
        safe = sanitize_text(raw)
        if safe != raw:
            updates.append((safe, identity))
    if updates:
        conn.executemany(
            f"UPDATE {table} SET {value_column}=? WHERE {id_column}=?", updates
        )


def _scrub_legacy_persistent_secrets(
    conn: sqlite3.Connection, tables: set[str]
) -> None:
    text_columns = (
        ("tasks", "id", "error"),
        ("tasks", "id", "status_text"),
        ("logs", "id", "message"),
        ("account_restrictions", "account_id", "message"),
        ("comment_schedule", "id", "result"),
        ("join_schedule", "id", "result"),
        ("comment_campaigns", "id", "pause_reason"),
        ("join_campaigns", "id", "pause_reason"),
        ("direct_message_deliveries", "id", "error"),
        ("comment_deliveries", "id", "error"),
    )
    for table, id_column, value_column in text_columns:
        if table in tables:
            _sanitize_text_column(conn, table, id_column, value_column)

    if "account_restrictions" in tables:
        columns = _column_names(conn, "account_restrictions")
        if {"account_id", "details_json"} <= columns:
            rows = conn.execute(
                "SELECT account_id, details_json FROM account_restrictions "
                "WHERE details_json IS NOT NULL AND details_json<>''"
            ).fetchall()
            updates = []
            for account_id, value in rows:
                raw = str(value or "{}")
                safe = sanitize_json(raw)
                if safe != raw:
                    updates.append((safe, account_id))
            if updates:
                conn.executemany(
                    "UPDATE account_restrictions SET details_json=? WHERE account_id=?",
                    updates,
                )


def migrate_rpc_cooldown_boot_guard_v26(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Persist boot-aware monotonic metadata for account RPC cooldowns."""

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
        if "account_rpc_cooldowns" in tables:
            columns = _column_names(conn, "account_rpc_cooldowns")
            if "boot_id" not in columns:
                conn.execute(
                    "ALTER TABLE account_rpc_cooldowns "
                    "ADD COLUMN boot_id TEXT NOT NULL DEFAULT ''"
                )
            if "steady_deadline" not in columns:
                conn.execute(
                    "ALTER TABLE account_rpc_cooldowns ADD COLUMN steady_deadline REAL"
                )
            if "fallback_wait_seconds" not in columns:
                conn.execute(
                    "ALTER TABLE account_rpc_cooldowns "
                    "ADD COLUMN fallback_wait_seconds INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(fallback_wait_seconds >= 1)"
                )

            # Recover the original server-requested duration from persisted
            # timestamps. This remains useful even if the current wall clock was
            # corrected after the row was written.
            conn.execute(
                """UPDATE account_rpc_cooldowns
                   SET fallback_wait_seconds=MAX(
                           1,
                           CAST(
                               ABS(
                                   (julianday(next_allowed_at) - julianday(updated_at))
                                   * 86400.0
                               ) AS INTEGER
                           ) + 1
                       )
                   WHERE fallback_wait_seconds IS NULL
                      OR fallback_wait_seconds <= 1"""
            )
        _scrub_legacy_persistent_secrets(conn, tables)

        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        if migrations is not None:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(26)")
        conn.execute("PRAGMA user_version = 26")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
