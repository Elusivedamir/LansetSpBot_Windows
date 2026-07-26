from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

import core.logging_setup as logging_setup
import core.secret_store as secret_store_module
from core.local_security import LocalFileSecurityError
from core.paths import AppPaths
from core.secret_store import SecretStore
from gui.views.account_view import AccountView
from gui.views.links_view import LinksView
from services.proxy_validation import normalize_proxy_config
from services.telegram_service import TelegramService
from storage.database import Database, DatabaseError


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _app_paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def test_secret_store_rejects_symlink_without_reading_external_target(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"telegram.api_hash": "outside-secret"}), encoding="utf-8"
    )
    link = tmp_path / ".secrets.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RuntimeError, match="unsafe|corrupted"):
        SecretStore(link).get_strict_optional("telegram.api_hash")

    assert outside.read_text(encoding="utf-8") == json.dumps(
        {"telegram.api_hash": "outside-secret"}
    )


def test_secret_store_rejects_non_string_and_oversized_values(tmp_path):
    target = tmp_path / ".secrets.json"
    target.write_text(
        json.dumps({"telegram.api_hash": {"nested": True}}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="corrupted, unavailable, or belongs to another OS profile"):
        SecretStore(target).get_strict_optional("telegram.api_hash")

    target.write_bytes(b"{" + b" " * (SecretStore.MAX_STORE_BYTES + 1) + b"}")
    with pytest.raises(RuntimeError, match="corrupted, unavailable, or belongs to another OS profile"):
        SecretStore(target).get_strict_optional("telegram.api_hash")


def test_secret_store_hardens_existing_world_readable_file(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode assertion")
    target = tmp_path / ".secrets.json"
    target.write_text(json.dumps({"telegram.api_hash": "secret"}), encoding="utf-8")
    target.chmod(0o777)

    assert SecretStore(target).get_strict_optional("telegram.api_hash") == "secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_secret_store_replace_failure_preserves_old_bytes(monkeypatch, tmp_path):
    target = tmp_path / ".secrets.json"
    # The store is fail-closed and only accepts its own authenticated format,
    # so the fixture must be written through SecretStore itself.
    SecretStore(target).set("token", "old")
    original = target.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(secret_store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        SecretStore(target).set("token", "new")

    assert target.read_bytes() == original
    assert list(tmp_path.glob("..secrets.json.*.tmp")) == []


def test_app_paths_reject_managed_symlink_outside_root(tmp_path):
    root = tmp_path / "Marlen"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "logs").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(LocalFileSecurityError, match="symbolic-link"):
        _app_paths(root).ensure()

    assert list(outside.iterdir()) == []


def test_database_and_sidecars_are_owner_only_under_common_umask(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode assertion")
    old_umask = os.umask(0o022)
    try:
        database = Database(tmp_path / "marlen.db")
        with database.get_connection() as connection:
            connection.execute("SELECT 1")
        database.close_thread_connection()
    finally:
        os.umask(old_umask)

    for candidate in tmp_path.glob("marlen.db*"):
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600


def test_database_rejects_symlink_target(tmp_path):
    outside = tmp_path / "outside.db"
    sqlite3.connect(outside).close()
    link = tmp_path / "marlen.db"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(DatabaseError, match="Unsafe SQLite path"):
        Database(link)


def test_logging_uses_private_file_and_rejects_symlink(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode assertion")
    paths = _app_paths(tmp_path / "data")
    monkeypatch.setattr(logging_setup, "APP_PATHS", paths)
    logger = logging_setup.setup_logging()
    logger.info("mode check")
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_file = paths.logs / "marlen.log"
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "baseFilename", "") == str(log_file):
            logging.getLogger().removeHandler(handler)
            handler.close()
    log_file.unlink()
    outside = tmp_path / "outside.log"
    outside.write_text("do not touch", encoding="utf-8")
    log_file.symlink_to(outside)
    with pytest.raises(RuntimeError, match="Unsafe local log file"):
        logging_setup.setup_logging()
    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_log_permission_failure_is_explicit(monkeypatch, tmp_path):
    paths = _app_paths(tmp_path / "data")
    monkeypatch.setattr(logging_setup, "APP_PATHS", paths)
    monkeypatch.setattr(logging_setup, "harden_private_file", lambda _path: False)
    with pytest.raises(PermissionError, match="restrict private log"):
        logging_setup.setup_logging()


@pytest.mark.parametrize(
    "host",
    [
        "https://proxy.example",
        "user:pass@proxy.example",
        "host/path",
        "bad\nhost",
        "bad host",
    ],
)
def test_proxy_rejects_url_credentials_paths_and_control_host(host):
    with pytest.raises(ValueError):
        normalize_proxy_config("SOCKS5", host, 1080, "user", "password")


@pytest.mark.parametrize("port", [0, -1, 65536, "not-a-number"])
def test_proxy_rejects_invalid_ports(port):
    with pytest.raises(ValueError):
        normalize_proxy_config("SOCKS5", "localhost", port)


def test_proxy_accepts_ipv4_ipv6_unicode_and_non_shell_passwords():
    assert normalize_proxy_config("SOCKS5", "127.0.0.1", 1080).host == "127.0.0.1"
    assert normalize_proxy_config("HTTP", "[::1]", "8080").host == "::1"
    assert (
        normalize_proxy_config("SOCKS4", "пример.рф", 9050).host
        == "xn--e1afmkfd.xn--p1ai"
    )
    password = "quoted'; & $(not-a-command) `still-data`"
    assert (
        normalize_proxy_config("SOCKS5", "localhost", 1080, "u", password).password
        == password
    )


def test_proxy_rejects_control_characters_in_password():
    with pytest.raises(ValueError, match="управляющие символы"):
        normalize_proxy_config("SOCKS5", "localhost", 1080, "user", "secret\nnext")


def test_telegram_build_proxy_uses_normalized_host():
    settings = SimpleNamespace(
        proxy_enabled=True,
        proxy_type="socks5",
        proxy_host="ПРИМЕР.РФ",
        proxy_port="1080",
        proxy_username=" user ",
        proxy_password="secret",
    )
    built = TelegramService.build_proxy(settings)
    assert built[1:] == ("xn--e1afmkfd.xn--p1ai", 1080, True, "user", "secret")


def test_account_view_clears_one_time_code_hash_and_2fa_after_success(tmp_path):
    app = _app()
    adapter = MagicMock()
    adapter.get_settings.return_value = {}
    adapter.set_auth_in_progress.return_value = None
    config = SimpleNamespace(
        telegram=SimpleNamespace(session_dir=tmp_path / "sessions"),
        database_path=tmp_path / "marlen.db",
    )
    view = AccountView(adapter, config)
    QThreadPool.globalInstance().waitForDone(5_000)
    app.processEvents()
    view.code.setText("12345")
    view.two_fa.setText("two-factor-secret")
    view.phone_code_hash = "hash-secret"
    view._authorized({"id": 1, "name": "Account", "_persisted": True})

    assert view.code.text() == ""
    assert view.two_fa.text() == ""
    assert view.phone_code_hash == ""
    view.deleteLater()
    app.processEvents()


def test_links_view_does_not_rebuild_10000_row_table_for_same_progress():
    app = _app()
    channels = [
        {"channel_id": index, "title": f"Channel {index}", "target_kind": "channel"}
        for index in range(10_000)
    ]
    adapter = MagicMock()
    adapter.get_channels.return_value = channels
    # Task updates that do not belong to the selected account are ignored.
    adapter.get_current_account_id.return_value = 909
    view = LinksView(adapter)
    view.total = len(channels)
    view.load_channels = MagicMock()

    payload = {"account_id": 909}
    for _ in range(20):
        view._task_changed({"status": "running", "progress": 1, "payload": payload})
    view._task_changed({"status": "running", "progress": 2, "payload": payload})

    assert view.load_channels.call_count == 2
    view.deleteLater()
    app.processEvents()


def test_corrupt_secret_file_diagnostics_do_not_echo_secret_values(
    monkeypatch, tmp_path
):
    paths = _app_paths(tmp_path / "data")
    monkeypatch.setattr(logging_setup, "APP_PATHS", paths)
    logger = logging_setup.setup_logging()
    secret_values = [
        "+491701234567",
        "api-hash-unique-4721",
        "proxy-password-unique-4721",
        "login-code-839201",
        "two-factor-unique-4721",
        "https://t.me/+privateUnique4721",
        "private comment text unique 4721",
    ]
    payload = {"telegram.api_hash": {"unexpected": secret_values}}
    store_path = tmp_path / "unsafe-secrets.json"
    store_path.write_text(json.dumps(payload), encoding="utf-8")
    store_path.chmod(0o600)

    with pytest.raises(RuntimeError, match="corrupted, unavailable, or belongs to another OS profile"):
        SecretStore(store_path).get_strict_optional("telegram.api_hash")
    logger.error("Safe diagnostic code: local_secret_invalid")
    log_file = paths.logs / "marlen.log"
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_file.read_text(encoding="utf-8")
    for secret in secret_values:
        assert secret not in content

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "baseFilename", "") == str(log_file):
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_telegram_session_rejects_symlink_and_hardens_healthy_file(tmp_path):
    outside = tmp_path / "outside.session"
    sqlite3.connect(outside).close()
    link = tmp_path / "main.session"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises(RuntimeError, match="Unsafe Telegram session"):
        TelegramService._prepare_session_file(link)

    link.unlink()
    healthy = tmp_path / "healthy.session"
    connection = sqlite3.connect(healthy)
    connection.execute("CREATE TABLE session_state(id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    if os.name != "nt":
        healthy.chmod(0o644)
    TelegramService._prepare_session_file(healthy)
    if os.name != "nt":
        assert stat.S_IMODE(healthy.stat().st_mode) == 0o600


def test_unsecured_quarantined_session_is_removed_fail_closed(tmp_path, monkeypatch):
    source = tmp_path / "broken.session"
    source.write_bytes(b"not a sqlite database")
    source.chmod(0o600)

    monkeypatch.setattr(
        "services.telegram_session.harden_private_file", lambda _path: False
    )

    with pytest.raises(RuntimeError, match="unsafe copy was removed"):
        TelegramService._prepare_session_file(source)

    assert not source.exists()
    assert list(tmp_path.glob("broken.session.corrupt.*")) == []


def test_secret_store_rejects_hardlink_to_external_file(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"telegram.api_hash": "outside-secret"}), encoding="utf-8"
    )
    outside.chmod(0o600)
    inside = tmp_path / ".secrets.json"
    try:
        os.link(outside, inside)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable")

    with pytest.raises(RuntimeError, match="corrupted, unavailable, or belongs to another OS profile"):
        SecretStore(inside).get_strict_optional("telegram.api_hash")
    assert outside.read_text(encoding="utf-8") == json.dumps(
        {"telegram.api_hash": "outside-secret"}
    )


def test_database_rejects_hardlink_to_external_file(tmp_path):
    outside = tmp_path / "outside.db"
    sqlite3.connect(outside).close()
    inside = tmp_path / "marlen.db"
    try:
        os.link(outside, inside)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable")

    before = outside.read_bytes()
    with pytest.raises(DatabaseError, match="hard-linked|Unsafe SQLite path"):
        Database(inside)
    assert outside.read_bytes() == before
