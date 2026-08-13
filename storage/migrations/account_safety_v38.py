from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3


def migrate_account_safety_v38(
    path: Path, *, sqlite_timeout_seconds: float, busy_timeout_ms: int
) -> None:
    """Create account-scoped adaptive safety state and evidence journal."""
    conn = sqlite3.connect(str(path), timeout=sqlite_timeout_seconds, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS account_safety_state(
                account_id INTEGER PRIMARY KEY,
                adaptive_level TEXT NOT NULL DEFAULT 'normal'
                    CHECK(adaptive_level IN ('normal','conservative','soft_protective')),
                last_flood_at DATETIME,
                flood_count_window INTEGER NOT NULL DEFAULT 0 CHECK(flood_count_window >= 0),
                recovery_not_before DATETIME,
                next_task_at DATETIME,
                last_reserved_task_id INTEGER,
                last_reserved_task_at DATETIME,
                next_mutation_at DATETIME,
                last_mutation_request TEXT,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id) REFERENCES telegram_accounts(telegram_account_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS account_safety_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                from_level TEXT NOT NULL,
                to_level TEXT NOT NULL,
                code TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(account_id) REFERENCES telegram_accounts(telegram_account_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_account_safety_recovery
                ON account_safety_state(adaptive_level, recovery_not_before);
            CREATE INDEX IF NOT EXISTS idx_account_safety_events_account_time
                ON account_safety_events(account_id, occurred_at DESC, id DESC);
            INSERT OR IGNORE INTO migrations(version) VALUES(38);
            PRAGMA user_version = 38;
            COMMIT;
            """
        )
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
