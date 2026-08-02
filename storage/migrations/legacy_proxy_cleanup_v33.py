from __future__ import annotations

from pathlib import Path

from storage.sqlcipher_driver import dbapi as sqlite3

_HISTORICAL_REMOVED_TYPES = ("MTPROXY", "MTPROTO", "MT-PROXY", "MT PROXY")
_SUPPORTED_TYPES = frozenset({"SOCKS5", "SOCKS4", "HTTP"})
_PUBLIC_PROXY_KEYS = (
    "telegram.proxy_enabled",
    "telegram.proxy_type",
    "telegram.proxy_host",
    "telegram.proxy_port",
    "telegram.proxy_username",
    "telegram.proxy_password",
    "telegram.proxy_secret",
)


def migrate_legacy_proxy_cleanup_v33(
    path: Path,
    *,
    sqlite_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Disable retired proxy profiles and remove obsolete SQLite credentials."""

    conn = sqlite3.connect(
        str(path), timeout=sqlite_timeout_seconds, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        global_rows = conn.execute(
            """SELECT key, value FROM settings
               WHERE key IN (
                   'telegram.proxy_enabled', 'telegram.proxy_type',
                   'telegram.proxy_host', 'telegram.proxy_port',
                   'telegram.proxy_username', 'telegram.proxy_password',
                   'telegram.proxy_secret'
               )"""
        ).fetchall()
        global_profile = {
            str(row["key"]): str(row["value"] or "")
            for row in global_rows
        }
        global_type = global_profile.get("telegram.proxy_type", "").strip().upper()
        global_secret = global_profile.get("telegram.proxy_secret", "").strip()
        global_removed = (
            global_type in _HISTORICAL_REMOVED_TYPES
            or (bool(global_type) and global_type not in _SUPPORTED_TYPES)
            or (not global_type and bool(global_secret))
        )
        if global_removed:
            conn.executemany(
                "DELETE FROM settings WHERE key=?",
                ((key,) for key in _PUBLIC_PROXY_KEYS),
            )
        else:
            conn.execute(
                "DELETE FROM settings WHERE key='telegram.proxy_secret'"
            )

        account_rows = conn.execute(
            """SELECT account_id, key, value FROM account_settings
               WHERE key IN (
                   'telegram.proxy_enabled', 'telegram.proxy_type',
                   'telegram.proxy_host', 'telegram.proxy_port',
                   'telegram.proxy_username', 'telegram.proxy_password',
                   'telegram.proxy_secret'
               )
               ORDER BY account_id"""
        ).fetchall()
        account_profiles: dict[int, dict[str, str]] = {}
        for row in account_rows:
            account_profiles.setdefault(int(row["account_id"]), {})[
                str(row["key"])
            ] = str(row["value"] or "")
        for account_id, profile in account_profiles.items():
            proxy_type = profile.get("telegram.proxy_type", "").strip().upper()
            proxy_secret = profile.get("telegram.proxy_secret", "").strip()
            removed = (
                proxy_type in _HISTORICAL_REMOVED_TYPES
                or (bool(proxy_type) and proxy_type not in _SUPPORTED_TYPES)
                or (not proxy_type and bool(proxy_secret))
            )
            if removed:
                conn.executemany(
                    "DELETE FROM account_settings WHERE account_id=? AND key=?",
                    ((account_id, key) for key in _PUBLIC_PROXY_KEYS),
                )
            else:
                conn.execute(
                    """DELETE FROM account_settings
                       WHERE account_id=? AND key='telegram.proxy_secret'""",
                    (account_id,),
                )
        conn.execute("INSERT OR IGNORE INTO migrations(version) VALUES(33)")
        conn.execute("PRAGMA user_version = 33")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
