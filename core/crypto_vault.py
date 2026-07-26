"""OS-bound authenticated encryption for local application secrets.

The master key never lives beside the protected data in plaintext:

* Windows stores a random key wrapped by current-user DPAPI.
* Other platforms fail closed unless an explicit test-only key is supplied.

Payloads use AES-256-GCM and purpose-separated HKDF subkeys.  The format is
versioned so a future release can migrate it without guessing.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Final, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.local_security import (
    LocalFileSecurityError,
    ensure_private_directory,
    harden_private_file,
    validate_private_regular_file,
)
from core.paths import APP_PATHS


class VaultError(RuntimeError):
    """Base class for local encryption/storage failures."""


class VaultUnavailableError(VaultError):
    """The OS-bound master-key store cannot be used safely."""


class VaultIntegrityError(VaultError):
    """An encrypted value is malformed, corrupted, or bound to another key."""


class MasterKeyProvider(Protocol):
    """Structural type for every master-key source used by the codec."""

    def get_or_create(self) -> bytes: ...


class StaticMasterKeyProvider:
    """Deterministic provider for tests and explicit dependency injection only."""

    def __init__(self, key: bytes) -> None:
        key = bytes(key)
        if len(key) != 32:
            raise ValueError("master key must contain exactly 32 bytes")
        self._key = key

    def get_or_create(self) -> bytes:
        return self._key


class OSBoundMasterKeyProvider:
    """Load or create one 256-bit master key bound to the current OS account."""

    WINDOWS_KEY_FILENAME: Final[str] = ".master-key.dpapi"
    WINDOWS_MAGIC: Final[bytes] = b"LSPDPAPI1\x00"
    TEST_KEY_ENV: Final[str] = "LANSETSPBOT_TEST_MASTER_KEY_B64"
    TEST_ENABLE_ENV: Final[str] = "LANSETSPBOT_ALLOW_TEST_MASTER_KEY"

    def __init__(self, storage_dir: Path | None = None) -> None:
        self.storage_dir = Path(storage_dir or APP_PATHS.root)
        self._cached: bytes | None = None
        self._lock = threading.RLock()

    def get_or_create(self) -> bytes:
        with self._lock:
            if self._cached is not None:
                return self._cached
            if os.name == "nt":
                key = self._windows_get_or_create()
            else:
                key = self._test_only_key()
            if len(key) != 32:
                raise VaultUnavailableError("OS master key has an invalid length")
            self._cached = bytes(key)
            return self._cached

    def delete(self) -> None:
        """Remove the durable master-key reference after a committed reset.

        The operation is deliberately separate from ciphertext deletion so a
        rollback-capable caller can remove data first and rotate the key only
        after the destructive transaction is committed.
        """

        with self._lock:
            if os.name == "nt":
                path = self.storage_dir / self.WINDOWS_KEY_FILENAME
                try:
                    exists = path.exists() or path.is_symlink()
                except OSError as exc:
                    raise VaultUnavailableError(
                        f"Could not inspect wrapped master-key file {path}: {exc}"
                    ) from exc
                if exists:
                    validate_private_regular_file(path, max_bytes=16 * 1024)
                    path.unlink()
            else:
                # The explicitly enabled synthetic key exists only in the test
                # process environment and has no durable OS record to remove.
                self._test_only_key()
            self._cached = None

    def _test_only_key(self) -> bytes:
        if os.getenv(self.TEST_ENABLE_ENV, "").strip() != "1":
            raise VaultUnavailableError(
                "Encrypted local storage is supported on Windows. "
                "This platform may only use an explicitly enabled synthetic test key."
            )
        encoded = os.getenv(self.TEST_KEY_ENV, "").strip()
        if not encoded:
            raise VaultUnavailableError("Synthetic test master key is missing")
        try:
            key = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001 - normalize decoding failure
            raise VaultUnavailableError("Synthetic test master key is invalid") from exc
        if len(key) != 32:
            raise VaultUnavailableError(
                "Synthetic test master key must decode to exactly 32 bytes"
            )
        return key

    @staticmethod
    def _write_new_private_file_exclusive(path: Path, payload: bytes) -> None:
        """Create ``path`` once without replacing a concurrent winner."""

        ensure_private_directory(path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = -1
        created = False
        try:
            descriptor = os.open(path, flags, 0o600)
            created = True
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if not harden_private_file(path):
                raise VaultUnavailableError(
                    f"Could not restrict wrapped master-key file {path}"
                )
            if os.name != "nt":
                directory_flags = os.O_RDONLY
                if hasattr(os, "O_DIRECTORY"):
                    directory_flags |= os.O_DIRECTORY
                if hasattr(os, "O_CLOEXEC"):
                    directory_flags |= os.O_CLOEXEC
                directory_fd = os.open(path.parent, directory_flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_windows_wrapped_key(
        self, path: Path, *, retries: int = 50, delay_seconds: float = 0.01
    ) -> bytes:
        """Read a completely committed DPAPI wrapper during first-run races."""

        last_error: BaseException | None = None
        for attempt in range(max(1, int(retries))):
            try:
                validate_private_regular_file(path, max_bytes=16 * 1024)
                payload = path.read_bytes()
                if payload.startswith(self.WINDOWS_MAGIC) and len(payload) > len(
                    self.WINDOWS_MAGIC
                ):
                    return self._dpapi_unprotect(payload[len(self.WINDOWS_MAGIC) :])
                last_error = VaultUnavailableError(
                    "Wrapped master-key file has an invalid format"
                )
            except (OSError, LocalFileSecurityError) as exc:
                last_error = exc
            if attempt + 1 < max(1, int(retries)):
                time.sleep(max(0.0, float(delay_seconds)))
        if isinstance(last_error, VaultUnavailableError):
            raise last_error
        raise VaultUnavailableError(
            f"Wrapped master-key file is unsafe: {path}"
        ) from last_error

    def _windows_get_or_create(self) -> bytes:
        path = self.storage_dir / self.WINDOWS_KEY_FILENAME
        ensure_private_directory(self.storage_dir)
        try:
            exists = path.exists() or path.is_symlink()
        except OSError as exc:
            raise VaultUnavailableError(
                f"Could not inspect wrapped master-key file {path}: {exc}"
            ) from exc
        if exists:
            return self._read_windows_wrapped_key(path)

        key = os.urandom(32)
        wrapped = self._dpapi_protect(key)
        try:
            self._write_new_private_file_exclusive(
                path, self.WINDOWS_MAGIC + wrapped
            )
        except FileExistsError:
            # Another process won the first-run race. Its final path can become
            # visible before the buffered payload is fully flushed, so read it
            # through the bounded committed-file retry helper.
            return self._read_windows_wrapped_key(path)
        return key

    @staticmethod
    def _dpapi_protect(plaintext: bytes) -> bytes:
        if os.name != "nt":
            raise VaultUnavailableError("DPAPI is unavailable on this platform")
        return OSBoundMasterKeyProvider._dpapi_transform(plaintext, protect=True)

    @staticmethod
    def _dpapi_unprotect(ciphertext: bytes) -> bytes:
        if os.name != "nt":
            raise VaultUnavailableError("DPAPI is unavailable on this platform")
        return OSBoundMasterKeyProvider._dpapi_transform(ciphertext, protect=False)

    @staticmethod
    def _dpapi_transform(payload: bytes, *, protect: bool) -> bytes:
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        # ctypes.WinDLL and the Win32 error helpers exist only on Windows,
        # which is the only platform that reaches this branch.
        windll_loader = ctypes.WinDLL  # type: ignore[attr-defined]
        crypt32 = windll_loader("crypt32", use_last_error=True)
        kernel32 = windll_loader("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        raw = bytes(payload)
        input_buffer = ctypes.create_string_buffer(raw, len(raw))
        input_blob = DataBlob(
            len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        entropy_bytes = b"LansetSpBot local vault v1"
        entropy_buffer = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
        entropy_blob = DataBlob(
            len(entropy_bytes),
            ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = DataBlob()
        description = wintypes.LPWSTR()
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "LansetSpBot local encryption key",
                ctypes.byref(entropy_blob),
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        if not ok:
            win_error = ctypes.WinError  # type: ignore[attr-defined]
            last_error = ctypes.get_last_error  # type: ignore[attr-defined]
            raise VaultUnavailableError(
                f"Windows DPAPI operation failed: {win_error(last_error())}"
            )
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)
            if description:
                kernel32.LocalFree(description)

class EncryptedBlobCodec:
    """Versioned AES-GCM codec with independent keys for each data purpose."""

    MAGIC: Final[bytes] = b"LSPBV1\x00"
    NONCE_BYTES: Final[int] = 12
    TAG_BYTES: Final[int] = 16
    HKDF_SALT: Final[bytes] = b"LansetSpBot authenticated local vault v1"

    def __init__(self, provider: MasterKeyProvider | None = None) -> None:
        self.provider = provider or OSBoundMasterKeyProvider()

    def is_encrypted(self, payload: bytes | bytearray | memoryview) -> bool:
        return bytes(payload).startswith(self.MAGIC)

    def _purpose_key(self, purpose: str) -> bytes:
        normalized = str(purpose).strip()
        if not normalized or len(normalized.encode("utf-8")) > 512:
            raise ValueError("encryption purpose is invalid")
        master = bytes(self.provider.get_or_create())
        if len(master) != 32:
            raise VaultUnavailableError("master key must contain exactly 32 bytes")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.HKDF_SALT,
            info=("LansetSpBot/" + normalized).encode("utf-8"),
        ).derive(master)

    def derive_key(self, *, purpose: str) -> bytes:
        """Return one purpose-separated 256-bit key for trusted local subsystems."""

        return self._purpose_key(purpose)

    def encrypt(self, plaintext: bytes, *, purpose: str) -> bytes:
        plaintext = bytes(plaintext)
        nonce = os.urandom(self.NONCE_BYTES)
        aad = self.MAGIC + str(purpose).encode("utf-8")
        ciphertext = AESGCM(self._purpose_key(purpose)).encrypt(nonce, plaintext, aad)
        return self.MAGIC + nonce + ciphertext

    def decrypt(self, payload: bytes, *, purpose: str) -> bytes:
        payload = bytes(payload)
        minimum = len(self.MAGIC) + self.NONCE_BYTES + self.TAG_BYTES
        if len(payload) < minimum or not payload.startswith(self.MAGIC):
            raise VaultIntegrityError("encrypted value has an invalid format")
        nonce_start = len(self.MAGIC)
        nonce_end = nonce_start + self.NONCE_BYTES
        nonce = payload[nonce_start:nonce_end]
        ciphertext = payload[nonce_end:]
        aad = self.MAGIC + str(purpose).encode("utf-8")
        try:
            return AESGCM(self._purpose_key(purpose)).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise VaultIntegrityError(
                "encrypted value failed authentication; it may be corrupted or copied from another profile"
            ) from exc

    @staticmethod
    def fingerprint(payload: bytes) -> str:
        """Non-secret diagnostic fingerprint for comparing encrypted artifacts."""
        return hashlib.sha256(bytes(payload)).hexdigest()[:16]
