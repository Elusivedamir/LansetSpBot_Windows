from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from core.crypto_vault import (
    EncryptedBlobCodec,
    OSBoundMasterKeyProvider,
    StaticMasterKeyProvider,
    VaultIntegrityError,
)
from core.secret_store import SecretStore


def _codec(byte: int = 7) -> EncryptedBlobCodec:
    return EncryptedBlobCodec(StaticMasterKeyProvider(bytes([byte]) * 32))


def test_encrypted_blob_roundtrip_and_purpose_separation():
    codec = _codec()
    plaintext = b"sk-test-not-a-real-key"
    encrypted = codec.encrypt(plaintext, purpose="one")

    assert encrypted.startswith(codec.MAGIC)
    assert plaintext not in encrypted
    assert codec.decrypt(encrypted, purpose="one") == plaintext
    with pytest.raises(VaultIntegrityError):
        codec.decrypt(encrypted, purpose="two")


def test_encrypted_blob_detects_single_byte_tampering():
    codec = _codec()
    encrypted = bytearray(codec.encrypt(b"payload", purpose="test"))
    encrypted[-1] ^= 1

    with pytest.raises(VaultIntegrityError):
        codec.decrypt(bytes(encrypted), purpose="test")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission-bit assertion")
def test_secret_store_never_persists_plaintext(tmp_path: Path):
    path = tmp_path / ".secrets.json"
    store = SecretStore(path, codec=_codec())
    secret = "sk-test-not-a-real-key"

    store.set("openai.api_key", secret)

    raw = path.read_bytes()
    assert raw.startswith(EncryptedBlobCodec.MAGIC)
    assert secret.encode() not in raw
    assert SecretStore(path, codec=_codec()).get_strict_optional(
        "openai.api_key"
    ) == secret
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_secret_store_migrates_legacy_plaintext_atomically(tmp_path: Path):
    path = tmp_path / ".secrets.json"
    path.write_text(
        json.dumps({"telegram.api_hash": "legacy-secret"}), encoding="utf-8"
    )
    if os.name != "nt":
        os.chmod(path, 0o600)

    store = SecretStore(path, codec=_codec())
    assert store.get_strict_optional("telegram.api_hash") == "legacy-secret"

    raw = path.read_bytes()
    assert raw.startswith(EncryptedBlobCodec.MAGIC)
    assert b"legacy-secret" not in raw
    assert list(tmp_path.glob("..secrets.json.*.tmp")) == []


def test_secret_store_fails_closed_with_wrong_key_or_tampering(tmp_path: Path):
    path = tmp_path / ".secrets.json"
    SecretStore(path, codec=_codec(1)).set("token", "secret")

    with pytest.raises(RuntimeError, match="unavailable"):
        SecretStore(path, codec=_codec(2)).get_strict_optional("token")

    damaged = bytearray(path.read_bytes())
    damaged[-1] ^= 1
    path.write_bytes(damaged)
    if os.name != "nt":
        os.chmod(path, 0o600)
    with pytest.raises(RuntimeError, match="unavailable"):
        SecretStore(path, codec=_codec(1)).get_strict_optional("token")


def test_secret_snapshot_validation_happens_before_encryption(tmp_path: Path):
    store = SecretStore(tmp_path / ".secrets.json", codec=_codec())
    with pytest.raises(ValueError):
        store.replace_snapshot({"key": object()})
    assert not store.fallback_path.exists()


def test_windows_wrapped_master_key_file_never_contains_raw_key(
    tmp_path: Path, monkeypatch
):
    def wrap(value: bytes) -> bytes:
        return b"TEST-WRAP\x00" + bytes(byte ^ 0xA5 for byte in value)

    def unwrap(value: bytes) -> bytes:
        assert value.startswith(b"TEST-WRAP\x00")
        return bytes(byte ^ 0xA5 for byte in value[len(b"TEST-WRAP\x00") :])

    monkeypatch.setattr(OSBoundMasterKeyProvider, "_dpapi_protect", staticmethod(wrap))
    monkeypatch.setattr(OSBoundMasterKeyProvider, "_dpapi_unprotect", staticmethod(unwrap))

    first = OSBoundMasterKeyProvider(tmp_path)
    key = first._windows_get_or_create()
    wrapped_path = tmp_path / first.WINDOWS_KEY_FILENAME
    stored = wrapped_path.read_bytes()

    assert len(key) == 32
    assert stored.startswith(first.WINDOWS_MAGIC)
    assert key not in stored
    assert OSBoundMasterKeyProvider(tmp_path)._windows_get_or_create() == key


def test_concurrent_windows_first_run_uses_one_durable_master_key(
    tmp_path: Path, monkeypatch
):
    barrier = threading.Barrier(2)

    def wrap(value: bytes) -> bytes:
        barrier.wait(timeout=5)
        return b"TEST-WRAP\x00" + bytes(byte ^ 0x5A for byte in value)

    def unwrap(value: bytes) -> bytes:
        assert value.startswith(b"TEST-WRAP\x00")
        return bytes(byte ^ 0x5A for byte in value[len(b"TEST-WRAP\x00") :])

    monkeypatch.setattr(OSBoundMasterKeyProvider, "_dpapi_protect", staticmethod(wrap))
    monkeypatch.setattr(OSBoundMasterKeyProvider, "_dpapi_unprotect", staticmethod(unwrap))

    results: list[bytes] = []
    errors: list[BaseException] = []

    def create() -> None:
        try:
            results.append(OSBoundMasterKeyProvider(tmp_path)._windows_get_or_create())
        except BaseException as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert OSBoundMasterKeyProvider(tmp_path)._windows_get_or_create() == results[0]
