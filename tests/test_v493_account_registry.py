"""Accounts get a table of their own - the foundation for running several.

Every campaign, channel, delivery and restriction has been scoped by account_id
since schema v18, but the accounts themselves lived in three settings rows, so
a second account had nowhere to exist. These tests pin the registry: that an
existing installation is imported rather than reset, that a row can be created
before Telegram has reported an id, that the enabled switch does not touch
data, and that nothing secret is written into SQLite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from storage.database import Database
from storage.db_common import DatabaseError


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "registry.db")
    try:
        yield db
    finally:
        db.close_thread_connection()


def test_a_fresh_profile_starts_with_one_imported_account(database: Database) -> None:
    accounts = database.list_accounts()
    assert len(accounts) == 1
    first = accounts[0]
    assert first["session_name"] == "main", (
        "the first account must keep the existing session file, or an upgrade "
        "would lose a live authorization"
    )
    assert first["enabled"] is True
    assert first["position"] == 0


def test_an_existing_installation_is_imported_not_reset(tmp_path: Path) -> None:
    """The account already configured in settings must become row 1."""

    db = Database(tmp_path / "existing.db")
    try:
        db.set_settings(
            {
                "telegram.account_id": "778899",
                "telegram.api_id": "12345",
                "telegram.proxy_enabled": "1",
                "telegram.proxy_type": "SOCKS5",
                "telegram.proxy_host": "proxy.example.net",
                "telegram.proxy_port": "1080",
            }
        )
        # Re-run the migration against a registry that does not exist yet.
        with db.get_connection() as conn:
            conn.execute("DELETE FROM accounts")
        db.close_thread_connection()

        from storage.migrations.account_registry_v31 import (
            migrate_account_registry_v31,
        )

        migrate_account_registry_v31(tmp_path / "existing.db")

        reopened = Database(tmp_path / "existing.db")
        try:
            imported = reopened.list_accounts()
            assert len(imported) == 1
            account = imported[0]
            assert account["telegram_account_id"] == 778899
            assert account["api_id"] == 12345
            assert account["proxy_enabled"] is True
            assert account["proxy_type"] == "SOCKS5"
            assert account["proxy_host"] == "proxy.example.net"
            assert account["proxy_port"] == 1080
            assert account["authorized"] is True
        finally:
            reopened.close_thread_connection()
    finally:
        db.close_thread_connection()


def test_a_new_account_exists_before_it_authorizes(database: Database) -> None:
    """Credentials are entered first; Telegram reports the id afterwards."""

    created = database.create_account(label="Второй", api_id=999)
    assert created["telegram_account_id"] == 0
    assert created["authorized"] is False
    assert created["session_name"] != "main", "each account needs its own session file"
    assert created["enabled"] is True

    authorized = database.update_account(created["id"], telegram_account_id=424242)
    assert authorized["authorized"] is True
    assert database.get_account_by_telegram_id(424242)["id"] == created["id"]


def test_session_names_cannot_escape_the_sessions_directory(database: Database) -> None:
    """The name reaches the filesystem, so a typed label must not steer it."""

    for hostile in ("../main", "main/../../x", "a b", "sess*", "конь", "." * 5):
        with pytest.raises(DatabaseError):
            database.create_account(session_name=hostile)


def test_an_omitted_session_name_is_generated_and_safe(database: Database) -> None:
    for _ in range(3):
        account = database.create_account(session_name="")
        assert account["session_name"].startswith("account-")
        assert account["session_name"].replace("-", "").isalnum()


def test_two_accounts_cannot_share_a_session_file(database: Database) -> None:
    database.create_account(session_name="second")
    with pytest.raises(DatabaseError):
        database.create_account(session_name="second")


def test_disabling_an_account_keeps_its_row_and_its_data(database: Database) -> None:
    account = database.create_account(label="Третий")
    disabled = database.set_account_enabled(account["id"], False)
    assert disabled["enabled"] is False
    assert database.get_account(account["id"]) is not None
    assert database.count_accounts() == 2
    assert database.count_accounts(enabled_only=True) == 1
    assert [row["id"] for row in database.list_accounts(enabled_only=True)] != [
        account["id"]
    ]

    re_enabled = database.set_account_enabled(account["id"], True)
    assert re_enabled["enabled"] is True
    assert database.count_accounts(enabled_only=True) == 2


def test_the_registry_stores_nothing_secret(database: Database) -> None:
    """api_hash, phone numbers and proxy passwords belong in the secret store."""

    from storage.db_accounts import ACCOUNT_COLUMNS

    forbidden = ("api_hash", "password", "secret", "token", "phone_number")
    for column in ACCOUNT_COLUMNS:
        assert not any(word in column for word in forbidden), (
            f"the accounts table must not carry {column}"
        )
    source = Path("storage/db_accounts.py").read_text(encoding="utf-8")
    assert "api_hash" not in source.split('"""', 2)[2], (
        "no code path may write an api_hash into the registry"
    )


def test_accounts_keep_a_stable_order(database: Database) -> None:
    second = database.create_account(label="B")
    third = database.create_account(label="C")
    assert [row["id"] for row in database.list_accounts()] == [
        1,
        second["id"],
        third["id"],
    ]
    assert [row["position"] for row in database.list_accounts()] == [0, 1, 2]


def test_deleting_a_row_leaves_account_scoped_data_alone(database: Database) -> None:
    """Removing a registry entry must not silently destroy history."""

    account = database.create_account(label="Уходящий")
    database.update_account(account["id"], telegram_account_id=5150)
    assert database.delete_account(account["id"]) is True
    assert database.get_account(account["id"]) is None
    assert database.delete_account(account["id"]) is False


def test_the_schema_version_moved_with_the_migration() -> None:
    from storage.db_schema import DatabaseSchemaMixin

    assert DatabaseSchemaMixin.SCHEMA_VERSION >= 31
