from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path


_LEGACY_PREFIX = "telegram.restriction."


def _legacy_setting(conn: sqlite3.Connection, suffix: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (f"{_LEGACY_PREFIX}{suffix}",)
    ).fetchone()
    return str(row[0] if row is not None and row[0] is not None else default)


def migrate_account_restrictions_v19(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Store Telegram restrictions independently for every account.

    V10 kept one restriction snapshot in global settings. That was sufficient for
    one selected session, but sequential account switching could overwrite the
    previous account's safety state. V19 moves the state to a row keyed by the
    immutable Telegram user id and migrates the legacy snapshot atomically.
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
            """CREATE TABLE IF NOT EXISTS account_restrictions(
                   account_id INTEGER PRIMARY KEY,
                   active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                   code TEXT NOT NULL,
                   message TEXT,
                   detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   checked_at DATETIME,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_restrictions_active "
            "ON account_restrictions(active, updated_at)"
        )

        # V15 allowed only one active join campaign in the whole database.
        # Scope that invariant to the owning account so future multi-account
        # dispatchers can operate independently without weakening per-account
        # single-campaign safety.
        has_join_campaigns = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='join_campaigns'"
        ).fetchone()
        if has_join_campaigns is not None:
            conn.execute("DROP INDEX IF EXISTS uq_join_campaign_active")
            conn.execute("DROP INDEX IF EXISTS uq_join_campaign_active_account")
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_join_campaign_active_account
                   ON join_campaigns(account_id)
                   WHERE status IN ('running','paused','network_wait')"""
            )

        # Preserve a V10 restriction before deleting the global keys. Account 0
        # is retained only as a fail-safe legacy/unauthenticated state; normal
        # Telegram work always provides a positive immutable user id.
        legacy_active = _legacy_setting(conn, "active", "0") == "1"
        if legacy_active:
            try:
                account_id = int(_legacy_setting(conn, "account_id", "0") or 0)
            except (TypeError, ValueError, OverflowError):
                account_id = 0
            conn.execute(
                """INSERT INTO account_restrictions(
                       account_id, active, code, message, detected_at, checked_at,
                       details_json, updated_at)
                   VALUES(?, 1, ?, ?, COALESCE(NULLIF(?, ''), CURRENT_TIMESTAMP),
                          NULLIF(?, ''), COALESCE(NULLIF(?, ''), '{}'), CURRENT_TIMESTAMP)
                   ON CONFLICT(account_id) DO UPDATE SET
                       active=1,
                       code=excluded.code,
                       message=excluded.message,
                       detected_at=excluded.detected_at,
                       checked_at=excluded.checked_at,
                       details_json=excluded.details_json,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    account_id,
                    _legacy_setting(conn, "code", "legacy_restriction")
                    or "legacy_restriction",
                    _legacy_setting(conn, "message", ""),
                    _legacy_setting(conn, "detected_at", ""),
                    _legacy_setting(conn, "checked_at", ""),
                    _legacy_setting(conn, "details", "{}"),
                ),
            )

        conn.execute("DELETE FROM settings WHERE key LIKE ?", (f"{_LEGACY_PREFIX}%",))
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(19)")
        conn.execute("PRAGMA user_version = 19")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
