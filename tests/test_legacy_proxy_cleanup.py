from __future__ import annotations

from pathlib import Path

from services.legacy_proxy_cleanup import purge_removed_proxy_credentials
from storage.database import Database
from storage.migrations.legacy_proxy_cleanup_v33 import (
    migrate_legacy_proxy_cleanup_v33,
)


class MemorySecretStore:
    def __init__(self, values=None) -> None:
        self.values = dict(values or {})

    def export_snapshot(self):
        return dict(self.values)

    def replace_snapshot(self, payload):
        self.values = dict(payload)


def test_legacy_proxy_profile_is_silently_disabled_and_credentials_removed(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "legacy-proxy.db")
    for account_id in (1001, 1002, 1003):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=f"Account {account_id}",
        )
    with db.get_connection() as conn:
        for key, value in {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_type": "MTPROXY",
            "telegram.proxy_host": "old.example",
            "telegram.proxy_port": "443",
            "telegram.proxy_secret": "legacy-secret",
            "commenting.daily_limit": "40",
        }.items():
            conn.execute(
                """INSERT INTO account_settings(account_id, key, value)
                   VALUES(1001, ?, ?)""",
                (key, value),
            )
    with db.get_connection() as conn:
        for key, value in {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_type": "SOCKS5",
            "telegram.proxy_host": "127.0.0.1",
            "telegram.proxy_port": "1080",
            "telegram.proxy_secret": "stale-standard-secret",
        }.items():
            conn.execute(
                """INSERT INTO account_settings(account_id, key, value)
                   VALUES(1002, ?, ?)""",
                (key, value),
            )
        for key, value in {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_host": "partial-old.example",
            "telegram.proxy_port": "443",
            "telegram.proxy_secret": "partial-secret",
        }.items():
            conn.execute(
                """INSERT INTO account_settings(account_id, key, value)
                   VALUES(1003, ?, ?)""",
                (key, value),
            )
    with db.get_connection() as conn:
        for key, value in {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_type": "mtproxy",
            "telegram.proxy_host": "global-old.example",
            "telegram.proxy_secret": "global-secret",
        }.items():
            conn.execute(
                """INSERT INTO settings(key, value) VALUES(?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    migrate_legacy_proxy_cleanup_v33(
        db.path,
        sqlite_timeout_seconds=db.sqlite_timeout_seconds,
        busy_timeout_ms=db.busy_timeout_ms,
    )

    removed = db.get_account_settings(1001)
    assert "telegram.proxy_type" not in removed
    assert "telegram.proxy_host" not in removed
    assert "telegram.proxy_secret" not in removed
    assert removed["commenting.daily_limit"] == "40"

    supported = db.get_account_settings(1002)
    assert supported["telegram.proxy_type"] == "SOCKS5"
    assert supported["telegram.proxy_host"] == "127.0.0.1"
    assert "telegram.proxy_secret" not in supported
    partial = db.get_account_settings(1003)
    assert "telegram.proxy_host" not in partial
    assert "telegram.proxy_secret" not in partial
    assert db.get_setting("telegram.proxy_type", "") == ""
    assert db.get_telegram_account(1001)["session_name"] == "account_1001"
    assert db.get_setting(
        "internal.removed_proxy_secret_cleanup_complete", ""
    ) == ""

    store = MemorySecretStore(
        {
            "telegram.proxy_secret": "global-secret",
            "account.1001.telegram.proxy_secret": "account-secret",
            "account.9999.telegram.proxy_secret": "orphan-secret",
            "account.1001.telegram.api_hash": "keep-api-hash",
        }
    )
    first = purge_removed_proxy_credentials(db, store)
    second = purge_removed_proxy_credentials(db, store)
    assert first == {"completed": True, "removed": 3}
    assert second == {"completed": True, "removed": 0}
    assert db.get_setting(
        "internal.removed_proxy_secret_cleanup_complete", ""
    ) == "1"
    assert store.values == {
        "account.1001.telegram.api_hash": "keep-api-hash"
    }
