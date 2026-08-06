from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3


def _create_lease_table(conn, name: str) -> None:
    conn.execute(
        f"""CREATE TABLE {name}(
               account_id INTEGER PRIMARY KEY,
               activity TEXT NOT NULL CHECK(activity IN ('warmup')),
               owner_token TEXT NOT NULL,
               started_at DATETIME NOT NULL,
               heartbeat_at DATETIME NOT NULL,
               lease_until DATETIME NOT NULL,
               metadata_json TEXT NOT NULL DEFAULT '{{}}',
               FOREIGN KEY(account_id)
                   REFERENCES telegram_accounts(telegram_account_id)
                   ON DELETE CASCADE
           )"""
    )


def migrate_account_activity_lease_fk_v35(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Repair early v34 installs and bind leases to real Telegram accounts."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        # SQLite cannot toggle foreign_keys inside an active transaction. The
        # temporary OFF state is used only while rebuilding this local table.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        table_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='account_activity_leases'"""
        ).fetchone()
        if table_exists is None:
            _create_lease_table(conn, "account_activity_leases")
        else:
            foreign_keys = conn.execute(
                "PRAGMA foreign_key_list(account_activity_leases)"
            ).fetchall()
            has_account_fk = any(
                str(row["table"]) == "telegram_accounts"
                and str(row["from"]) == "account_id"
                and str(row["to"]) == "telegram_account_id"
                and str(row["on_delete"]).upper() == "CASCADE"
                for row in foreign_keys
            )
            if not has_account_fk:
                conn.execute("DROP TABLE IF EXISTS account_activity_leases_v35")
                _create_lease_table(conn, "account_activity_leases_v35")
                conn.execute(
                    """INSERT INTO account_activity_leases_v35(
                           account_id, activity, owner_token, started_at,
                           heartbeat_at, lease_until, metadata_json)
                       SELECT l.account_id, l.activity, l.owner_token, l.started_at,
                              l.heartbeat_at, l.lease_until, l.metadata_json
                       FROM account_activity_leases l
                       JOIN telegram_accounts a
                         ON a.telegram_account_id=l.account_id
                       WHERE l.activity='warmup'"""
                )
                conn.execute("DROP TABLE account_activity_leases")
                conn.execute(
                    "ALTER TABLE account_activity_leases_v35 "
                    "RENAME TO account_activity_leases"
                )

        # Remove stale rows even when an early v34 table already declared the
        # correct foreign key but was populated while foreign_keys was disabled.
        # A lease without its Telegram account must never survive migration.
        conn.execute(
            """DELETE FROM account_activity_leases
               WHERE NOT EXISTS(
                   SELECT 1 FROM telegram_accounts a
                   WHERE a.telegram_account_id=account_activity_leases.account_id
               )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_account_activity_leases_until
               ON account_activity_leases(lease_until)"""
        )
        violations = conn.execute(
            "PRAGMA foreign_key_check(account_activity_leases)"
        ).fetchall()
        if violations:
            raise RuntimeError(
                "Foreign-key verification failed during account activity migration"
            )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(35)")
        conn.execute("PRAGMA user_version = 35")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
