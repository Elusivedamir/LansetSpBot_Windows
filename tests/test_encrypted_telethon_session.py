from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from core.crypto_vault import EncryptedBlobCodec, StaticMasterKeyProvider
from services.encrypted_telethon_session import (
    EncryptedSQLiteSession,
    TelegramSessionEncryptionError,
)


def _codec(byte: int) -> EncryptedBlobCodec:
    return EncryptedBlobCodec(StaticMasterKeyProvider(bytes([byte]) * 32))


def _raw_keys(path: Path) -> tuple[bytes, bytes]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT auth_key, tmp_auth_key FROM sessions"
        ).fetchone()
    assert row is not None
    return bytes(row[0] or b""), bytes(row[1] or b"")


def test_plaintext_telethon_keys_migrate_and_reopen(tmp_path: Path):
    base = tmp_path / "main"
    auth = b"A" * 256
    temporary = b"B" * 256

    legacy = SQLiteSession(str(base), store_tmp_auth_key_on_disk=True)
    legacy.auth_key = AuthKey(auth)
    legacy.tmp_auth_key = AuthKey(temporary)
    legacy.save()
    legacy.close()
    before_auth, before_tmp = _raw_keys(base.with_suffix(".session"))
    assert before_auth == auth
    assert before_tmp == temporary

    encrypted = EncryptedSQLiteSession(
        base, store_tmp_auth_key_on_disk=True, codec=_codec(3)
    )
    assert encrypted.auth_key.key == auth
    # Telethon 1.44.0 declares MemorySession.tmp_auth_key's setter with
    # @auth_key.setter, so the public tmp_auth_key getter returns the auth key.
    # Assert the real attribute that both Telethon and this subclass persist.
    assert encrypted._tmp_auth_key.key == temporary
    encrypted.save()
    encrypted.close()

    stored_auth, stored_tmp = _raw_keys(base.with_suffix(".session"))
    assert stored_auth.startswith(EncryptedBlobCodec.MAGIC)
    assert stored_tmp.startswith(EncryptedBlobCodec.MAGIC)
    assert auth not in stored_auth and temporary not in stored_tmp

    reopened = EncryptedSQLiteSession(
        base, store_tmp_auth_key_on_disk=True, codec=_codec(3)
    )
    assert reopened.auth_key.key == auth
    assert reopened._tmp_auth_key.key == temporary
    reopened.set_dc(4, "149.154.167.91", 443)
    reopened.save()
    assert reopened.auth_key.key == auth
    reopened.close()


def test_encrypted_telethon_session_rejects_wrong_profile_key(tmp_path: Path):
    base = tmp_path / "main"
    session = EncryptedSQLiteSession(base, codec=_codec(5))
    session.auth_key = AuthKey(b"C" * 256)
    session.save()
    session.close()

    with pytest.raises(TelegramSessionEncryptionError, match="another OS profile"):
        EncryptedSQLiteSession(base, codec=_codec(6))


def test_encrypted_session_rejects_ciphertext_tampering_without_overwrite(
    tmp_path: Path,
):
    base = tmp_path / "main"
    codec = _codec(23)
    session = EncryptedSQLiteSession(base, codec=codec)
    session.auth_key = AuthKey(b"T" * 256)
    session.save()
    session.close()

    path = base.with_suffix(".session")
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT auth_key FROM sessions").fetchone()
        assert row is not None
        stored = bytes(row[0] or b"")
        assert stored.startswith(EncryptedBlobCodec.MAGIC)
        tampered = bytearray(stored)
        tampered[-1] ^= 0x01
        connection.execute("UPDATE sessions SET auth_key=?", (bytes(tampered),))
        connection.commit()

    with pytest.raises(
        TelegramSessionEncryptionError,
        match="corrupted|another OS profile",
    ):
        EncryptedSQLiteSession(base, codec=codec)

    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT auth_key FROM sessions").fetchone()
        assert row is not None
        assert bytes(row[0] or b"") == bytes(tampered)


def test_invalid_legacy_key_is_not_silently_replaced(tmp_path: Path):
    base = tmp_path / "main"
    legacy = SQLiteSession(str(base))
    legacy.auth_key = AuthKey(b"too-short")
    legacy.save()
    legacy.close()

    with pytest.raises(TelegramSessionEncryptionError, match="unknown format"):
        EncryptedSQLiteSession(base, codec=_codec(7))


def test_runtime_clients_receive_encrypted_session_instance():
    telegram_source = Path("services/telegram_service.py").read_text(encoding="utf-8")
    auth_source = Path("gui/auth_worker.py").read_text(encoding="utf-8")

    assert "EncryptedSQLiteSession(telegram_session_base)" in telegram_source
    normalized_telegram_source = " ".join(telegram_source.split())
    assert "PacedTelegramClient( encrypted_session," in normalized_telegram_source
    assert "EncryptedSQLiteSession(self._session_base())" in auth_source
    normalized_auth_source = " ".join(auth_source.split())
    assert "TelegramClient( encrypted_session," in normalized_auth_source
