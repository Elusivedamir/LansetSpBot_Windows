from __future__ import annotations

from pathlib import Path

from core.account_limits import MAX_REGISTERED_TELEGRAM_ACCOUNTS
from storage.sqlcipher_driver import dbapi as sqlite3


def migrate_account_limit_v32(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Replace the historical five-account trigger with the product limit."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TRIGGER IF EXISTS telegram_accounts_limit_insert")
        conn.execute(
            f"""CREATE TRIGGER telegram_accounts_limit_insert
                BEFORE INSERT ON telegram_accounts
                WHEN (SELECT COUNT(*) FROM telegram_accounts) >= {MAX_REGISTERED_TELEGRAM_ACCOUNTS}
                 AND NOT EXISTS(
                     SELECT 1 FROM telegram_accounts
                      WHERE telegram_account_id=NEW.telegram_account_id
                 )
                BEGIN
                    SELECT RAISE(ABORT, 'telegram account limit reached');
                END"""
        )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(32)")
        conn.execute("PRAGMA user_version = 32")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
