from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3


def migrate_account_registry_v31(
    path: str | Path,
    *,
    sqlite_timeout_seconds: float = 30.0,
    busy_timeout_ms: int = 30_000,
) -> None:
    """Give accounts a table of their own.

    Every campaign, channel, delivery and restriction has been scoped by
    account_id since v18, but the accounts themselves were never stored: the
    single account lived in the settings rows telegram.api_id, telegram.phone
    and telegram.account_id, so a second one had nowhere to exist.

    The registry adds what those rows cannot express - one row per account,
    each with its own API credentials, proxy, Telegram session file and an
    enabled flag - while leaving the account-scoped data untouched, because it
    is keyed by the Telegram account id that this table also records.

    Secrets stay out of SQLite: api_hash, phone and proxy credentials live in
    the secret store under per-account keys. The OpenAI key is deliberately not
    per account - one key serves the whole program.

    The existing account is imported as row 1 and keeps the session file name
    "main", so nothing on disk has to move and an upgrade cannot lose a live
    authorization.
    """

    conn = sqlite3.connect(str(path), timeout=float(sqlite_timeout_seconds))
    try:
        conn.execute(f"PRAGMA busy_timeout = {max(100, int(busy_timeout_ms))}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                -- 0 until the account authorizes and Telegram reports its id.
                -- Once set it is what every account-scoped table already uses.
                telegram_account_id INTEGER NOT NULL DEFAULT 0,
                label TEXT NOT NULL DEFAULT '',
                -- Telethon opens sessions/<session_name>.session. The imported
                -- account keeps "main" so no file has to be moved.
                session_name TEXT NOT NULL UNIQUE,
                api_id INTEGER,
                phone_hint TEXT NOT NULL DEFAULT '',
                proxy_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK(proxy_enabled IN (0,1)),
                proxy_type TEXT NOT NULL DEFAULT '',
                proxy_host TEXT NOT NULL DEFAULT '',
                proxy_port INTEGER,
                -- The operator switch: a disabled account keeps its data and
                -- runs nothing.
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                position INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_telegram_id
                ON accounts(telegram_account_id) WHERE telegram_account_id <> 0;
            CREATE INDEX IF NOT EXISTS idx_accounts_enabled
                ON accounts(enabled, position, id);
            """
        )

        existing = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
        if not existing or int(existing[0] or 0) == 0:
            settings = {
                str(key): value
                for key, value in conn.execute(
                    "SELECT key, value FROM settings WHERE key LIKE 'telegram.%'"
                ).fetchall()
            }

            def _int(key: str) -> int | None:
                raw = str(settings.get(key) or "").strip()
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None

            telegram_account_id = _int("telegram.account_id") or 0
            api_id = _int("telegram.api_id")
            proxy_enabled = (
                1
                if str(settings.get("telegram.proxy_enabled") or "").strip().lower()
                in {"1", "true", "yes", "on"}
                else 0
            )
            conn.execute(
                """
                INSERT INTO accounts(
                    telegram_account_id, label, session_name, api_id, phone_hint,
                    proxy_enabled, proxy_type, proxy_host, proxy_port, enabled,
                    position
                ) VALUES(?, ?, 'main', ?, '', ?, ?, ?, ?, 1, 0)
                """,
                (
                    telegram_account_id,
                    str(settings.get("telegram.label") or "").strip(),
                    api_id,
                    proxy_enabled,
                    str(settings.get("telegram.proxy_type") or "").strip(),
                    str(settings.get("telegram.proxy_host") or "").strip(),
                    _int("telegram.proxy_port"),
                ),
            )

        migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        if migrations is not None:
            conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(31)")
        conn.execute("PRAGMA user_version = 31")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
