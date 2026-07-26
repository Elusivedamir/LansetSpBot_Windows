from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from core.crypto_vault import EncryptedBlobCodec, OSBoundMasterKeyProvider
from core.local_security import (
    LocalFileSecurityError,
    ensure_private_directory,
    harden_private_file,
    validate_private_regular_file,
)
from core.paths import APP_PATHS

log = logging.getLogger(__name__)


class SecretStore:
    """Store credentials in an authenticated, OS-bound encrypted local file.

    The file keeps its historical ``.secrets.json`` name for upgrade
    compatibility, but new contents are binary AES-GCM ciphertext.  The master
    key is protected by current-user DPAPI on Windows. Legacy
    plaintext JSON is migrated atomically after a successful strict read.
    """

    MAX_STORE_BYTES = 256 * 1024
    MAX_ENCRYPTED_STORE_BYTES = MAX_STORE_BYTES + 4096
    ENCRYPTION_PURPOSE = "secret-store.v1"
    MAX_ENTRIES = 64
    MAX_KEY_CHARS = 256
    MAX_VALUE_CHARS = 64 * 1024

    def __init__(
        self,
        fallback_path: Path | None = None,
        *,
        codec: EncryptedBlobCodec | None = None,
    ) -> None:
        self.fallback_path = fallback_path or (APP_PATHS.root / ".secrets.json")
        self._codec = codec or EncryptedBlobCodec(
            OSBoundMasterKeyProvider(self.fallback_path.parent)
        )
        self._cache: dict[str, str] = {}
        self._cache_loaded: set[str] = set()
        self._lock = threading.RLock()

    def set(self, key: str, value: str | None) -> None:
        key = str(key)
        value = "" if value is None else str(value)
        if not key or len(key) > self.MAX_KEY_CHARS:
            raise ValueError("Invalid local secret key")
        if len(value) > self.MAX_VALUE_CHARS:
            raise ValueError(f"Local secret value is too large for {key}")
        with self._lock:
            data = self._read_fallback(for_update=True)
            if value:
                data[key] = value
            else:
                data.pop(key, None)
            self._write_fallback(data)
            self._cache[key] = value
            self._cache_loaded.add(key)

    def get(self, key: str, default: str = "") -> str:
        """Compatibility read used only by legacy adapters.

        Production settings paths call :meth:`get_strict_optional`, which fails
        closed. This method preserves the historical default-on-corruption API
        for test doubles and older integrations while still refusing writes.
        """

        key = str(key)
        with self._lock:
            if key in self._cache_loaded:
                return self._cache.get(key, default) or default
            value = self._read_fallback().get(key, default)
            self._cache[key] = value
            self._cache_loaded.add(key)
            return value

    def get_strict_optional(self, key: str) -> str | None:
        """Read one credential without masking storage failures."""

        key = str(key)
        with self._lock:
            try:
                stored = self._read_fallback(for_update=True).get(key)
            except Exception as exc:
                raise RuntimeError(
                    f"Local secret storage is unavailable for {key}: {exc}"
                ) from exc
            value = None if stored is None else str(stored)
            self._cache[key] = value or ""
            self._cache_loaded.add(key)
            return value

    @classmethod
    def validate_snapshot(cls, payload: object) -> dict[str, str]:
        """Validate a complete secret-store snapshot without writing it."""

        if not isinstance(payload, dict):
            raise ValueError("secret store snapshot must be a JSON object")
        if len(payload) > cls.MAX_ENTRIES:
            raise ValueError("secret store snapshot contains too many entries")
        validated: dict[str, str] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(
                    "secret store snapshot keys and values must be strings"
                )
            if not key or len(key) > cls.MAX_KEY_CHARS:
                raise ValueError("secret store snapshot contains an invalid key")
            if len(value) > cls.MAX_VALUE_CHARS:
                raise ValueError(f"secret value is too large for {key}")
            validated[key] = value
        encoded = json.dumps(validated, ensure_ascii=False).encode("utf-8")
        if len(encoded) > cls.MAX_STORE_BYTES:
            raise ValueError("secret store snapshot is too large")
        return validated

    def export_snapshot(self) -> dict[str, str]:
        """Return a strict in-memory copy suitable for an encrypted/local backup."""

        with self._lock:
            return dict(self._read_fallback(for_update=True))

    def replace_snapshot(self, payload: object) -> None:
        """Atomically replace all stored credentials with a validated snapshot."""

        validated = self.validate_snapshot(payload)
        with self._lock:
            self._write_fallback(validated)
            self._cache = dict(validated)
            self._cache_loaded = set(validated)

    def delete(self, key: str) -> None:
        self.set(key, None)

    def _read_fallback(self, *, for_update: bool = False) -> dict[str, str]:
        legacy_plaintext = False
        try:
            try:
                exists = self.fallback_path.exists() or self.fallback_path.is_symlink()
            except OSError as exc:
                raise LocalFileSecurityError(
                    f"Could not inspect local secret store {self.fallback_path}: {exc}"
                ) from exc
            if not exists:
                return {}

            initial = validate_private_regular_file(
                self.fallback_path, max_bytes=self.MAX_ENCRYPTED_STORE_BYTES
            )
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.fallback_path, flags)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
                    raise LocalFileSecurityError(
                        "Local secret store changed while it was being opened"
                    )
                if opened.st_size > self.MAX_ENCRYPTED_STORE_BYTES:
                    raise LocalFileSecurityError(
                        f"Local secret store exceeds {self.MAX_ENCRYPTED_STORE_BYTES} bytes"
                    )
                with os.fdopen(descriptor, "rb", closefd=True) as handle:
                    descriptor = -1
                    raw = handle.read(self.MAX_ENCRYPTED_STORE_BYTES + 1)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

            if len(raw) > self.MAX_ENCRYPTED_STORE_BYTES:
                raise LocalFileSecurityError("Local secret store is too large")
            if self._codec.is_encrypted(raw):
                decoded = self._codec.decrypt(raw, purpose=self.ENCRYPTION_PURPOSE)
            else:
                # One-time compatibility path for releases that stored protected
                # but plaintext JSON. Unknown binary formats fail closed.
                if not raw.lstrip().startswith(b"{"):
                    raise ValueError("secret store has an unknown format")
                decoded = raw
                legacy_plaintext = True
            if len(decoded) > self.MAX_STORE_BYTES:
                raise ValueError("decrypted secret store is too large")
            payload = json.loads(decoded.decode("utf-8"))
            validated = self.validate_snapshot(payload)
            if legacy_plaintext and for_update:
                self._write_fallback(validated)
                log.info("Migrated legacy plaintext secret store to encrypted format")
            return validated
        except Exception as exc:
            log.exception("Could not read encrypted local secret store")
            if for_update:
                raise RuntimeError(
                    "Local secret store is corrupted, unavailable, or belongs to another OS profile; refusing to use it"
                ) from exc
            return {}

    def _write_fallback(self, data: dict[str, str]) -> None:
        validated = self.validate_snapshot(data)
        ensure_private_directory(self.fallback_path.parent)
        plaintext = json.dumps(validated, ensure_ascii=False).encode("utf-8")
        payload = self._codec.encrypt(
            plaintext, purpose=self.ENCRYPTION_PURPOSE
        )
        if len(payload) > self.MAX_ENCRYPTED_STORE_BYTES:
            raise ValueError("Encrypted local secret store is too large")
        fd = -1
        tmp: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.fallback_path.name}.",
                suffix=".tmp",
                dir=str(self.fallback_path.parent),
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            if not harden_private_file(tmp):
                raise RuntimeError(f"Could not restrict temporary secret store {tmp}")

            os.replace(tmp, self.fallback_path)
            tmp = None
            if not harden_private_file(self.fallback_path):
                self.fallback_path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Could not restrict local secret store {self.fallback_path}"
                )

            if os.name != "nt":
                flags = os.O_RDONLY
                if hasattr(os, "O_DIRECTORY"):
                    flags |= os.O_DIRECTORY
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                directory_fd = os.open(self.fallback_path.parent, flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    @staticmethod
    def _restrict_windows_acl(path: Path) -> bool:
        """Compatibility wrapper used by platform-specific validation tests."""
        if os.name != "nt":
            return True
        return harden_private_file(path)
