"""Telethon SQLite session with authenticated encryption for authorization keys."""

from __future__ import annotations

from typing import Final

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession
from telethon.sessions.memory import MemorySession

from core.crypto_vault import (
    EncryptedBlobCodec,
    OSBoundMasterKeyProvider,
    VaultIntegrityError,
)


class TelegramSessionEncryptionError(RuntimeError):
    """The durable Telegram authorization key cannot be used safely."""


class EncryptedSQLiteSession(SQLiteSession):
    """Keep Telethon caches in SQLite while encrypting reusable auth material.

    Telethon still owns the session schema and all entity/update caches. Only
    ``auth_key`` and ``tmp_auth_key`` are transformed, because those are the
    portable credentials that grant account access. Existing 256-byte plaintext
    keys are migrated in place after they are loaded successfully.
    """

    AUTH_PURPOSE: Final[str] = "telegram-session.auth-key.v1"
    TMP_AUTH_PURPOSE: Final[str] = "telegram-session.tmp-auth-key.v1"
    AUTH_KEY_BYTES: Final[int] = 256

    def __init__(
        self,
        session_id=None,
        store_tmp_auth_key_on_disk: bool = False,
        *,
        codec: EncryptedBlobCodec | None = None,
    ) -> None:
        # SQLiteSession may call our overridden _update_session_table while it
        # creates a fresh database, so the codec must exist first.
        storage_dir = None
        if session_id:
            from pathlib import Path

            storage_dir = Path(str(session_id)).expanduser().parent
            if storage_dir.name.lower() == "sessions":
                storage_dir = storage_dir.parent
        self._session_codec = codec or EncryptedBlobCodec(
            OSBoundMasterKeyProvider(storage_dir)
        )
        super().__init__(
            session_id,
            store_tmp_auth_key_on_disk=store_tmp_auth_key_on_disk,
        )
        self._load_and_migrate_authorization_keys()

    def _decode_key(self, raw: object, *, purpose: str) -> tuple[bytes, bool]:
        if raw in (None, b"", ""):
            return b"", False
        try:
            payload = bytes(raw)
        except Exception as exc:  # noqa: BLE001 - normalize SQLite driver types
            raise TelegramSessionEncryptionError(
                "Telegram session contains an unreadable authorization key"
            ) from exc
        migrated = False
        if self._session_codec.is_encrypted(payload):
            try:
                plaintext = self._session_codec.decrypt(payload, purpose=purpose)
            except VaultIntegrityError as exc:
                raise TelegramSessionEncryptionError(
                    "Telegram session authorization key is corrupted or belongs "
                    "to another OS profile"
                ) from exc
        elif len(payload) == self.AUTH_KEY_BYTES:
            plaintext = payload
            migrated = True
        else:
            raise TelegramSessionEncryptionError(
                "Telegram session authorization key has an unknown format"
            )
        if plaintext and len(plaintext) != self.AUTH_KEY_BYTES:
            raise TelegramSessionEncryptionError(
                "Telegram session authorization key has an invalid length"
            )
        return plaintext, migrated

    def _encode_key(self, auth_key: AuthKey | None, *, purpose: str) -> bytes:
        if not auth_key or not auth_key.key:
            return b""
        raw = bytes(auth_key.key)
        # During SQLiteSession construction an existing encrypted BLOB is
        # temporarily wrapped in AuthKey by Telethon before this subclass can
        # decrypt it. Preserve that authenticated BLOB if an internal schema
        # upgrade writes the session row during the parent constructor.
        if self._session_codec.is_encrypted(raw):
            return raw
        if len(raw) != self.AUTH_KEY_BYTES:
            raise TelegramSessionEncryptionError(
                "Refusing to persist an invalid Telegram authorization key"
            )
        return self._session_codec.encrypt(raw, purpose=purpose)

    def _load_and_migrate_authorization_keys(self) -> None:
        row = self._execute("select auth_key, tmp_auth_key from sessions")
        if not row:
            self._auth_key = None
            self._tmp_auth_key = None
            return
        auth_plain, auth_legacy = self._decode_key(
            row[0], purpose=self.AUTH_PURPOSE
        )
        tmp_plain, tmp_legacy = self._decode_key(
            row[1], purpose=self.TMP_AUTH_PURPOSE
        )
        self._auth_key = AuthKey(data=auth_plain) if auth_plain else None
        self._tmp_auth_key = AuthKey(data=tmp_plain) if tmp_plain else None
        if auth_legacy or tmp_legacy:
            self._update_session_table()
            self.save()

    def set_dc(self, dc_id, server_address, port):
        # SQLiteSession's implementation reads the just-written encrypted blob
        # back into AuthKey directly. Preserve Telethon's state transition while
        # avoiding that plaintext-only assumption.
        MemorySession.set_dc(self, dc_id, server_address, port)
        self._update_session_table()

    def _update_session_table(self):
        cursor = self._cursor()
        try:
            cursor.execute("delete from sessions")
            cursor.execute(
                "insert or replace into sessions values (?,?,?,?,?,?)",
                (
                    self._dc_id,
                    self._server_address,
                    self._port,
                    self._encode_key(self._auth_key, purpose=self.AUTH_PURPOSE),
                    self._takeout_id,
                    self._encode_key(
                        self._tmp_auth_key,
                        purpose=self.TMP_AUTH_PURPOSE,
                    )
                    if self.store_tmp_auth_key_on_disk
                    else b"",
                ),
            )
        finally:
            cursor.close()

    def clone(self, to_instance=None):
        if to_instance is None:
            to_instance = type(self)(
                None,
                store_tmp_auth_key_on_disk=self.store_tmp_auth_key_on_disk,
                codec=self._session_codec,
            )
        return super().clone(to_instance)
