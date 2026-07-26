"""SQLCipher's locked-memory mode must never take the process down.

Found on a user's Windows 11 machine with Python 3.14 x64: keying a database,
enabling PRAGMA cipher_memory_security and running fifty CREATE TABLE
statements terminated the interpreter with 0xC00000FD, a C stack overflow, and
no Python exception at all. VirtualLock was failing with error 1453
(ERROR_WORKING_SET_QUOTA) and SQLCipher 4.12.0 recursed instead of degrading.
The same script with the pragma off completed normally.

So the pragma is issued only where the OS has been shown to honour a lock.
"""

from __future__ import annotations

import logging

import pytest

from core import secure_memory
from storage import sqlcipher_driver


class FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConnection:
    """Records statements and answers PRAGMA cipher_version."""

    def __init__(self, version: str = "4.12.0 community"):
        self.statements: list[str] = []
        self._version = version

    def execute(self, statement, *args):
        self.statements.append(statement)
        if "cipher_version" in statement:
            return FakeCursor((self._version,))
        return FakeCursor(None)


KEY = bytes(range(32))


@pytest.fixture(autouse=True)
def _forget_probe_result():
    secure_memory.reset_cached_decision()
    yield
    secure_memory.reset_cached_decision()


def test_the_pragma_is_issued_when_the_os_can_lock_memory(monkeypatch) -> None:
    monkeypatch.setattr(sqlcipher_driver, "secure_memory_available", lambda: True)
    connection = FakeConnection()
    sqlcipher_driver._apply_key(connection, KEY)
    assert any("cipher_memory_security = ON" in s for s in connection.statements)


def test_the_pragma_is_skipped_when_the_os_cannot_lock_memory(monkeypatch) -> None:
    monkeypatch.setattr(sqlcipher_driver, "secure_memory_available", lambda: False)
    connection = FakeConnection()
    sqlcipher_driver._apply_key(connection, KEY)
    assert not any("cipher_memory_security" in s for s in connection.statements)


def test_the_key_and_the_sqlcipher_check_survive_either_decision(monkeypatch) -> None:
    """Skipping the lock must not skip encryption or the driver check."""

    for available in (True, False):
        monkeypatch.setattr(
            sqlcipher_driver, "secure_memory_available", lambda a=available: a
        )
        connection = FakeConnection()
        sqlcipher_driver._apply_key(connection, KEY)
        assert connection.statements[0].startswith("PRAGMA key = ")
        assert KEY.hex() in connection.statements[0]
        assert any("cipher_version" in s for s in connection.statements)


def test_a_driver_without_sqlcipher_is_still_refused(monkeypatch) -> None:
    monkeypatch.setattr(sqlcipher_driver, "secure_memory_available", lambda: False)
    with pytest.raises(sqlcipher_driver.SQLCipherUnavailableError):
        sqlcipher_driver._apply_key(FakeConnection(version=""), KEY)


def test_the_driver_never_enables_locked_memory_unconditionally() -> None:
    """Guard the shape of the fix, not just its behaviour."""

    from pathlib import Path

    source = Path(sqlcipher_driver.__file__).read_text(encoding="utf-8")
    guarded = (
        "    if secure_memory_available():\n"
        '        connection.execute("PRAGMA cipher_memory_security = ON")\n'
    )
    assert guarded in source, "the pragma must stay behind the capability check"
    assert source.count("cipher_memory_security = ON") == 1


def test_posix_needs_no_probe(monkeypatch) -> None:
    monkeypatch.setattr(secure_memory.sys, "platform", "linux")

    def explode(*_args, **_kwargs):
        raise AssertionError("the Windows probe must not run off Windows")

    monkeypatch.setattr(secure_memory, "_evaluate_windows", explode)
    assert secure_memory.secure_memory_available() is True


def test_windows_accepts_a_machine_that_can_already_lock(monkeypatch) -> None:
    monkeypatch.setattr(secure_memory.sys, "platform", "win32")
    monkeypatch.setattr(secure_memory, "_windows_probe", lambda size: (True, 0))

    def explode() -> bool:
        raise AssertionError("a working machine must not be reconfigured")

    monkeypatch.setattr(secure_memory, "_windows_raise_working_set", explode)
    assert secure_memory.secure_memory_available() is True


def test_windows_raises_the_working_set_before_giving_up(monkeypatch) -> None:
    monkeypatch.setattr(secure_memory.sys, "platform", "win32")
    attempts: list[int] = []

    def probe(size: int):
        attempts.append(size)
        return (len(attempts) > 1, secure_memory.ERROR_WORKING_SET_QUOTA)

    monkeypatch.setattr(secure_memory, "_windows_probe", probe)
    monkeypatch.setattr(
        secure_memory, "_windows_enable_working_set_privilege", lambda: True
    )
    monkeypatch.setattr(secure_memory, "_windows_raise_working_set", lambda: True)

    assert secure_memory.secure_memory_available() is True
    assert len(attempts) == 2, "the probe must be retried after the quota is raised"


def test_windows_refusal_is_reported_and_does_not_crash(monkeypatch, caplog) -> None:
    monkeypatch.setattr(secure_memory.sys, "platform", "win32")
    monkeypatch.setattr(
        secure_memory,
        "_windows_probe",
        lambda size: (False, secure_memory.ERROR_WORKING_SET_QUOTA),
    )
    monkeypatch.setattr(
        secure_memory, "_windows_enable_working_set_privilege", lambda: True
    )
    monkeypatch.setattr(secure_memory, "_windows_raise_working_set", lambda: True)

    with caplog.at_level(logging.WARNING, logger=secure_memory.log.name):
        assert secure_memory.secure_memory_available() is False
    assert any("still refuses to lock" in record.message for record in caplog.records)


def test_a_broken_probe_is_survivable(monkeypatch, caplog) -> None:
    """A failure to answer the question is not a reason to take the app down."""

    monkeypatch.setattr(secure_memory.sys, "platform", "win32")

    def explode(*_args, **_kwargs):
        raise OSError("ctypes is unavailable")

    monkeypatch.setattr(secure_memory, "_evaluate_windows", explode)
    with caplog.at_level(logging.WARNING, logger=secure_memory.log.name):
        assert secure_memory.secure_memory_available() is False
    assert any("Could not determine" in record.message for record in caplog.records)


def test_the_decision_is_taken_once(monkeypatch) -> None:
    monkeypatch.setattr(secure_memory.sys, "platform", "win32")
    calls: list[int] = []

    def probe(size: int):
        calls.append(size)
        return (True, 0)

    monkeypatch.setattr(secure_memory, "_windows_probe", probe)
    for _ in range(5):
        assert secure_memory.secure_memory_available() is True
    assert len(calls) == 1, "probing locks megabytes; it must not repeat per connection"


def test_the_probe_reserves_headroom_over_what_sqlcipher_needs() -> None:
    assert secure_memory.LOCK_PROBE_BYTES >= 4 * 1024 * 1024
    assert secure_memory.MINIMUM_WORKING_SET_BYTES > secure_memory.LOCK_PROBE_BYTES
    assert (
        secure_memory.MAXIMUM_WORKING_SET_BYTES
        >= secure_memory.MINIMUM_WORKING_SET_BYTES
    )
