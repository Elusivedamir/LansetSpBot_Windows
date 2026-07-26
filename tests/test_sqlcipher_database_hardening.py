from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.crypto_vault import EncryptedBlobCodec, StaticMasterKeyProvider
from core.secret_store import SecretStore
from storage import sqlcipher_driver as driver


class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self):
        self.commands: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def execute(self, sql: str, parameters=()):
        self.commands.append((sql, tuple(parameters)))
        if sql == "PRAGMA cipher_version":
            return _FakeCursor(("4.6.1",))
        if sql.startswith("SELECT count(*) FROM sqlite_master"):
            return _FakeCursor((1,))
        return _FakeCursor()

    def close(self):
        self.closed = True


class _FakeDriver:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection

    def connect(self, *args, **kwargs):
        del args, kwargs
        return self.connection


def test_sqlcipher_key_is_applied_before_first_schema_read(monkeypatch, tmp_path: Path):
    connection = _FakeConnection()
    monkeypatch.setattr(driver, "_DRIVER", _FakeDriver(connection))
    monkeypatch.setattr(driver, "SQLCIPHER_AVAILABLE", True)

    result = driver.connect_encrypted_database(
        tmp_path / "marlen.db", key=b"K" * 32
    )

    assert result is connection
    statements = [sql for sql, _params in connection.commands]
    assert statements[0].startswith('PRAGMA key = "x\'')
    assert statements.index("PRAGMA cipher_version") < statements.index(
        "SELECT count(*) FROM sqlite_master"
    )


def test_production_refuses_plaintext_sqlite_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(driver.TEST_PLAINTEXT_ENV, raising=False)
    monkeypatch.setattr(driver, "SQLCIPHER_AVAILABLE", False)
    monkeypatch.setattr(driver, "SQLCIPHER_IMPORT_ERROR", ImportError("missing"))

    with pytest.raises(driver.SQLCipherUnavailableError, match="refusing"):
        driver.connect_encrypted_database(tmp_path / "marlen.db", key=b"K" * 32)


def test_environment_flag_alone_cannot_enable_plaintext_fallback(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv(driver.TEST_PLAINTEXT_ENV, "1")
    monkeypatch.setattr(driver, "SQLCIPHER_AVAILABLE", False)
    monkeypatch.setattr(driver, "SQLCIPHER_IMPORT_ERROR", ImportError("missing"))
    monkeypatch.delitem(driver.sys.modules, "pytest", raising=False)

    assert driver.plaintext_test_mode_enabled() is False
    with pytest.raises(driver.SQLCipherUnavailableError, match="refusing"):
        driver.connect_encrypted_database(tmp_path / "marlen.db", key=b"K" * 32)


def test_frozen_build_cannot_enable_plaintext_fallback(monkeypatch):
    monkeypatch.setenv(driver.TEST_PLAINTEXT_ENV, "1")
    monkeypatch.setattr(driver.sys, "frozen", True, raising=False)

    assert driver.plaintext_test_mode_enabled() is False


def test_database_key_is_separated_from_secret_store_key():
    codec = EncryptedBlobCodec(StaticMasterKeyProvider(b"M" * 32))

    database_key = codec.derive_key(purpose=driver.DATABASE_KEY_PURPOSE)
    secret_key = codec.derive_key(purpose=SecretStore.ENCRYPTION_PURPOSE)

    assert len(database_key) == 32
    assert len(secret_key) == 32
    assert database_key != secret_key


def test_registered_key_preserves_custom_key_storage_binding(monkeypatch, tmp_path: Path):
    database = tmp_path / "stage" / "marlen.db"
    database.parent.mkdir()
    expected = b"R" * 32
    driver.register_database_key(database, expected)
    connection = _FakeConnection()
    monkeypatch.setattr(driver, "_DRIVER", _FakeDriver(connection))
    monkeypatch.setattr(driver, "SQLCIPHER_AVAILABLE", True)
    monkeypatch.setattr(
        driver,
        "derive_database_key",
        lambda _path: (_ for _ in ()).throw(AssertionError("must use registered key")),
    )

    driver.connect_encrypted_database(database)

    assert expected.hex() in connection.commands[0][0]
    driver.forget_database_key(database)




def test_pre_restore_snapshot_uses_profile_root_key_directory(tmp_path: Path):
    database = tmp_path / "profile" / "backups" / "pre-restore.db"

    assert driver.default_database_key_storage_dir(database) == tmp_path / "profile"

def test_existing_empty_database_is_not_silently_reinitialized(tmp_path: Path):
    database = tmp_path / "marlen.db"
    database.touch()
    if driver.os.name != "nt":
        database.chmod(0o600)

    with pytest.raises(driver.SQLCipherError, match="empty"):
        driver.prepare_encrypted_database(database, key_storage_dir=tmp_path)

def test_corrupted_migration_journal_fails_closed(tmp_path: Path):
    database = tmp_path / "marlen.db"
    journal, _temporary, _rollback = driver.migration_artifacts(database)
    journal.write_text("not-json", encoding="utf-8")
    if driver.os.name != "nt":
        journal.chmod(0o600)

    with pytest.raises(driver.DatabaseEncryptionMigrationError, match="corrupted"):
        driver.recover_database_encryption_migration(database, key=b"K" * 32)


def test_migration_journal_cannot_target_another_database(tmp_path: Path):
    database = tmp_path / "marlen.db"
    journal, _temporary, _rollback = driver.migration_artifacts(database)
    journal.write_text(
        json.dumps(
            {
                "version": driver.MIGRATION_VERSION,
                "state": "prepared",
                "database": "other.db",
            }
        ),
        encoding="utf-8",
    )
    if driver.os.name != "nt":
        journal.chmod(0o600)

    with pytest.raises(driver.DatabaseEncryptionMigrationError, match="another database"):
        driver.recover_database_encryption_migration(database, key=b"K" * 32)


def test_release_declares_sqlcipher_and_bundles_driver():
    requirements = Path("requirements-runtime.lock").read_text(encoding="utf-8")
    windows_spec = Path("build/LansetSpBot.windows.spec").read_text(encoding="utf-8")

    assert "sqlcipher3==0.6.2" in requirements
    assert 'collect_submodules("sqlcipher3")' in windows_spec


def test_factory_reset_tracks_every_sqlcipher_migration_artifact():
    source = Path("core/factory_reset.py").read_text(encoding="utf-8")

    assert "migration_artifacts(database_path)" in source
    assert "*migration_artifacts(database_path)" in source
