from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication
from telethon import connection

from gui.views.account_view import AccountView
from services.api import ServiceAPI
from services.proxy_validation import (
    SUPPORTED_PROXY_TYPES,
    normalize_proxy_config,
)
from services.telegram_service import TelegramService


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("proxy_type", ["SOCKS5", "SOCKS4", "HTTP"])
def test_standard_proxy_types_are_normalized(proxy_type: str) -> None:
    config = normalize_proxy_config(
        proxy_type,
        "127.0.0.1",
        "1080",
        "user",
        "password",
    )
    assert config.proxy_type == proxy_type
    assert config.host == "127.0.0.1"
    assert config.port == 1080
    assert config.username == "user"
    assert config.password == "password"


def test_only_standard_proxy_types_are_supported() -> None:
    assert SUPPORTED_PROXY_TYPES == {"SOCKS5", "SOCKS4", "HTTP"}
    with pytest.raises(ValueError, match="Неподдерживаемый тип"):
        normalize_proxy_config("MTPROXY", "127.0.0.1", 443)


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("", 1080),
        ("https://proxy.example", 1080),
        ("bad host", 1080),
        ("127.0.0.1", 0),
        ("127.0.0.1", 65536),
        ("127.0.0.1", "not-a-number"),
    ],
)
def test_invalid_proxy_coordinates_are_rejected(host, port) -> None:
    with pytest.raises(ValueError):
        normalize_proxy_config("SOCKS5", host, port)


def test_standard_proxy_credentials_remain_protected() -> None:
    assert "telegram.proxy_username" in ServiceAPI.SECRET_SETTING_KEYS
    assert "telegram.proxy_password" in ServiceAPI.SECRET_SETTING_KEYS
    assert "telegram.proxy_secret" not in ServiceAPI.SECRET_SETTING_KEYS


def test_disabled_proxy_uses_normal_tcp_connection() -> None:
    proxy, connection_type = TelegramService.build_transport(
        SimpleNamespace(proxy_enabled=False)
    )
    assert proxy is None
    assert connection_type is connection.ConnectionTcpFull


def test_account_view_has_no_removed_secret_widget(qapp, monkeypatch) -> None:
    monkeypatch.setattr(AccountView, "load_settings", lambda self: None)
    view = AccountView(SimpleNamespace(), SimpleNamespace())
    try:
        options = [
            view.proxy_type.itemText(index)
            for index in range(view.proxy_type.count())
        ]
        assert options == ["SOCKS5", "SOCKS4", "HTTP"]
        assert not hasattr(view, "proxy_secret")
    finally:
        view.deleteLater()
        qapp.processEvents()
