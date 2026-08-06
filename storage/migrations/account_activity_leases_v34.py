from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3


def migrate_account_activity_leases_v34(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Add a stale-safe per-account lease for warmup/campaign exclusion."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS account_activity_leases(
                   account_id INTEGER PRIMARY KEY,
                   activity TEXT NOT NULL CHECK(activity IN ('warmup')),
                   owner_token TEXT NOT NULL,
                   started_at DATETIME NOT NULL,
                   heartbeat_at DATETIME NOT NULL,
                   lease_until DATETIME NOT NULL,
                   metadata_json TEXT NOT NULL DEFAULT '{}',
                   FOREIGN KEY(account_id)
                       REFERENCES telegram_accounts(telegram_account_id)
                       ON DELETE CASCADE
               )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_account_activity_leases_until
               ON account_activity_leases(lease_until)"""
        )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(34)")
        conn.execute("PRAGMA user_version = 34")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
