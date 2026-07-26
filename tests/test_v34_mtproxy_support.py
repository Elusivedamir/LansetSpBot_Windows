from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import connection

from core.config import TelegramSettings
from core.redaction import sanitize_data, sanitize_text
from services.mtproxy_faketls import (
    ConnectionTcpMTProxyFakeTLS,
    FakeTLSStreamReader,
    FakeTLSStreamWriter,
    MTProxyFakeTLSClientHello,
)
from services.proxy_validation import normalize_mtproxy_secret, normalize_proxy_config
from services.telegram_service import TelegramService


RAW_SECRET = bytes(range(16))
HEX_SECRET = RAW_SECRET.hex()
BASE64_SECRET = base64.urlsafe_b64encode(RAW_SECRET).decode().rstrip("=")
EE_DOMAIN = b"example.com"
EE_HEX_SECRET = (b"\xee" + RAW_SECRET + EE_DOMAIN).hex()
EE_BASE64_SECRET = (
    base64.urlsafe_b64encode(b"\xee" + RAW_SECRET + EE_DOMAIN)
    .decode()
    .rstrip("=")
)


def _settings(**overrides):
    values = {
        "proxy_enabled": True,
        "proxy_type": "MTPROXY",
        "proxy_host": "173.209.232.237",
        "proxy_port": "443",
        "proxy_username": "legacy-user",
        "proxy_password": "legacy-password",
        "proxy_secret": EE_BASE64_SECRET,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mtproxy_secret_accepts_classic_dd_and_ee_formats():
    assert normalize_mtproxy_secret(HEX_SECRET) == HEX_SECRET
    assert normalize_mtproxy_secret("dd" + HEX_SECRET) == HEX_SECRET
    assert normalize_mtproxy_secret(BASE64_SECRET) == HEX_SECRET
    assert normalize_mtproxy_secret(EE_HEX_SECRET) == EE_HEX_SECRET
    assert normalize_mtproxy_secret(EE_BASE64_SECRET) == EE_HEX_SECRET

    dd_secret = base64.urlsafe_b64encode(b"\xdd" + RAW_SECRET).decode().rstrip("=")
    assert normalize_mtproxy_secret(dd_secret) == HEX_SECRET


def test_mtproxy_secret_does_not_treat_plain_16_byte_key_as_transport_marker():
    core_starting_with_ee = b"\xee" + bytes(range(15))
    core_starting_with_dd = b"\xdd" + bytes(range(15))
    ee_base64 = base64.urlsafe_b64encode(core_starting_with_ee).decode().rstrip("=")
    dd_base64 = base64.urlsafe_b64encode(core_starting_with_dd).decode().rstrip("=")

    assert normalize_mtproxy_secret(core_starting_with_ee.hex()) == core_starting_with_ee.hex()
    assert normalize_mtproxy_secret(core_starting_with_dd.hex()) == core_starting_with_dd.hex()
    assert normalize_mtproxy_secret(ee_base64) == core_starting_with_ee.hex()
    assert normalize_mtproxy_secret(dd_base64) == core_starting_with_dd.hex()


def test_mtproxy_secret_rejects_unknown_long_payload_instead_of_truncating():
    long_plain = base64.urlsafe_b64encode(RAW_SECRET + b"ignored").decode().rstrip("=")
    with pytest.raises(ValueError, match="длиннее 16 байт"):
        normalize_mtproxy_secret(long_plain)


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "abc",
        "00" * 15,
        "not base64 ***",
        "tg://proxy?server=x",
        "aa\nbb",
        "ee" + HEX_SECRET,  # no Fake-TLS domain
        (b"\xee" + RAW_SECRET + b"bad/domain").hex(),
    ],
)
def test_mtproxy_secret_rejects_malformed_values(secret):
    with pytest.raises(ValueError):
        normalize_mtproxy_secret(secret)


def test_mtproxy_config_uses_ee_secret_and_drops_socks_credentials():
    config = normalize_proxy_config(
        "mtproto",
        "173.209.232.237",
        443,
        "user",
        "password",
        EE_BASE64_SECRET,
    )
    assert config.proxy_type == "MTPROXY"
    assert config.host == "173.209.232.237"
    assert config.port == 443
    assert config.username == ""
    assert config.password == ""
    assert config.secret == EE_HEX_SECRET


def test_standard_proxy_drops_stale_mtproxy_secret():
    config = normalize_proxy_config(
        "SOCKS5", "127.0.0.1", 1080, "user", "password", EE_BASE64_SECRET
    )
    assert config.secret == ""
    assert config.username == "user"
    assert config.password == "password"


def test_mtproxy_build_transport_selects_fake_tls_for_ee_secret():
    proxy, connection_type = TelegramService.build_transport(_settings())
    assert proxy == ("173.209.232.237", 443, EE_HEX_SECRET)
    assert connection_type is ConnectionTcpMTProxyFakeTLS


def test_mtproxy_build_transport_keeps_classic_transport_for_plain_secret():
    proxy, connection_type = TelegramService.build_transport(
        _settings(proxy_secret=BASE64_SECRET)
    )
    assert proxy == ("173.209.232.237", 443, HEX_SECRET)
    assert connection_type is connection.ConnectionTcpMTProxyRandomizedIntermediate


def test_disabled_proxy_uses_normal_tcp_connection():
    proxy, connection_type = TelegramService.build_transport(
        _settings(proxy_enabled=False)
    )
    assert proxy is None
    assert connection_type is connection.ConnectionTcpFull


def test_fake_tls_client_hello_contains_sni_and_expected_record_size():
    packet = MTProxyFakeTLSClientHello(EE_HEX_SECRET).build()
    assert len(packet) == 517
    assert packet.startswith(b"\x16\x03\x01\x02\x00\x01")
    assert EE_DOMAIN in packet




def test_fake_tls_server_hello_authentication_is_verified():
    codec = MTProxyFakeTLSClientHello(EE_HEX_SECRET)
    codec.build()
    server_hello = bytearray(138)
    server_hello[:3] = b"\x16\x03\x03"
    server_hello[43] = 32
    server_hello[44:76] = codec._session_id
    server_hello[127:136] = b"\x14\x03\x03\x00\x01\x01\x17\x03\x03"
    server_hello[136:138] = b"\x00\x00"
    unsigned = bytes(server_hello[:11] + b"\x00" * 32 + server_hello[43:])
    server_hello[11:43] = hmac.new(
        codec.secret, codec._client_digest + unsigned, hashlib.sha256
    ).digest()

    codec.verify_server_hello(bytes(server_hello))


def test_fake_tls_connection_passes_only_core_key_to_telethon():
    loggers = defaultdict(lambda: logging.getLogger("test.mtproxy"))
    transport = ConnectionTcpMTProxyFakeTLS(
        "149.154.167.50",
        443,
        2,
        loggers=loggers,
        proxy=("proxy.example", 443, EE_HEX_SECRET),
    )
    assert transport._ip == "proxy.example"
    assert transport._port == 443
    assert transport._secret == RAW_SECRET
    assert transport._fake_tls_hello.domain == EE_DOMAIN


@pytest.mark.asyncio
async def test_fake_tls_stream_wrappers_frame_and_reassemble_data():
    class RawWriter:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

        def is_closing(self):
            return False

        def get_extra_info(self, name, default=None):
            return default

        def write_eof(self):
            return None

        transport = None

    payload = b"x" * 20000
    raw_writer = RawWriter()
    writer = FakeTLSStreamWriter(raw_writer)
    assert writer.write(payload) == len(payload)

    framed = bytes(raw_writer.data)
    offset = 0
    recovered = bytearray()
    while offset < len(framed):
        assert framed[offset : offset + 3] == b"\x17\x03\x03"
        length = int.from_bytes(framed[offset + 3 : offset + 5], "big")
        assert 0 < length <= 16384
        recovered.extend(framed[offset + 5 : offset + 5 + length])
        offset += 5 + length
    assert bytes(recovered) == payload

    class RawReader:
        def __init__(self, data):
            self.data = bytearray(data)

        async def readexactly(self, size):
            if len(self.data) < size:
                partial = bytes(self.data)
                self.data.clear()
                raise asyncio.IncompleteReadError(partial, size)
            result = bytes(self.data[:size])
            del self.data[:size]
            return result

        def at_eof(self):
            return not self.data

    reader = FakeTLSStreamReader(RawReader(framed))
    assert await reader.readexactly(123) == payload[:123]
    assert await reader.readexactly(len(payload) - 123) == payload[123:]


def test_main_telegram_service_receives_fake_tls_transport(tmp_path, monkeypatch):
    captured: dict = {}

    class Client:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr("services.telegram_service.PacedTelegramClient", Client)
    monkeypatch.setattr(
        TelegramService, "_prepare_session_file", lambda self, path: None
    )
    monkeypatch.setattr(
        TelegramService, "_secure_session_file", lambda self, path: None
    )

    TelegramService(
        TelegramSettings(
            api_id=1,
            api_hash="hash",
            session_dir=tmp_path,
            proxy_enabled=True,
            proxy_type="MTPROXY",
            proxy_host="173.209.232.237",
            proxy_port=443,
            proxy_secret=EE_BASE64_SECRET,
        ),
        limiter=object(),
    )

    assert captured["kwargs"]["proxy"] == (
        "173.209.232.237",
        443,
        EE_HEX_SECRET,
    )
    assert captured["kwargs"]["connection"] is ConnectionTcpMTProxyFakeTLS
    assert captured["kwargs"]["receive_updates"] is False


@pytest.mark.asyncio
async def test_auth_worker_uses_same_fake_tls_transport(tmp_path: Path, monkeypatch):
    import gui.auth_worker as auth_module
    from gui.auth_worker import TelegramAuthWorker

    captured: dict = {}

    class Client:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            self.connected = False

        async def connect(self):
            self.connected = True

        def is_connected(self):
            return self.connected

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(
                id=1, first_name="Test", last_name="", username="test", phone="1"
            )

        async def disconnect(self):
            self.connected = False

    monkeypatch.setattr(auth_module, "TelegramClient", Client)
    monkeypatch.setattr(
        TelegramService, "_prepare_session_file", staticmethod(lambda path: None)
    )
    monkeypatch.setattr(
        TelegramService, "_secure_session_file", staticmethod(lambda path: None)
    )

    worker = TelegramAuthWorker(
        mode="request_code",
        settings={
            "telegram.api_id": "123",
            "telegram.api_hash": "hash",
            "telegram.phone": "+10000000000",
            "telegram.proxy_enabled": "1",
            "telegram.proxy_type": "MTPROXY",
            "telegram.proxy_host": "173.209.232.237",
            "telegram.proxy_port": "443",
            "telegram.proxy_secret": EE_BASE64_SECRET,
        },
        session_dir=tmp_path,
    )
    await worker._run()

    assert captured["kwargs"]["proxy"] == (
        "173.209.232.237",
        443,
        EE_HEX_SECRET,
    )
    assert captured["kwargs"]["connection"] is ConnectionTcpMTProxyFakeTLS


def test_mtproxy_secret_is_redacted_from_text_and_structured_data():
    message = f"ConnectionError: proxy_secret={EE_HEX_SECRET}"
    safe = sanitize_text(message, secrets=(EE_HEX_SECRET,))
    assert EE_HEX_SECRET not in safe
    assert "<redacted>" in safe
    payload = sanitize_data({"mtproxy_secret": EE_HEX_SECRET, "error": message})
    assert payload["mtproxy_secret"] == "<redacted>"
    assert EE_HEX_SECRET not in str(payload)


def test_service_api_treats_mtproxy_secret_as_protected_credential():
    from services.api import ServiceAPI

    assert "telegram.proxy_secret" in ServiceAPI.SECRET_SETTING_KEYS


def test_mtproxy_secret_is_saved_outside_sqlite_and_loaded_back(tmp_path):
    from core.secret_store import SecretStore
    from services.api import ServiceAPI
    from storage.database import Database

    database = Database(tmp_path / "state.db")
    secret_store = SecretStore(tmp_path / ".secrets.json")
    api = ServiceAPI(database, queue_worker=None, secret_store=secret_store)

    api.save_settings(
        {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_type": "MTPROXY",
            "telegram.proxy_host": "173.209.232.237",
            "telegram.proxy_port": "443",
            "telegram.proxy_secret": EE_HEX_SECRET,
        }
    )

    assert database.get_setting("telegram.proxy_secret") is None
    assert secret_store.get_strict_optional("telegram.proxy_secret") == EE_HEX_SECRET
    assert api.get_settings("telegram.")["telegram.proxy_secret"] == EE_HEX_SECRET


def test_account_view_switches_to_mtproxy_secret_field_and_serializes_it(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from gui.views.account_view import AccountView

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(AccountView, "load_settings", lambda self: None)
    view = AccountView(SimpleNamespace(), SimpleNamespace())
    view.proxy_enabled.setChecked(True)
    view.proxy_type.setCurrentText("MTPROXY")
    view.api_id.setText("123")
    view.api_hash.setText("hash")
    view.phone.setText("+10000000000")
    view.proxy_host.setText("173.209.232.237")
    view.proxy_port.setText("443")
    view.proxy_secret.setText(EE_BASE64_SECRET)

    assert not view.proxy_secret.isHidden()
    assert view.proxy_login.isHidden()
    assert view.proxy_password.isHidden()

    values = view._settings()
    assert values["telegram.proxy_type"] == "MTPROXY"
    assert values["telegram.proxy_secret"] == EE_HEX_SECRET
    assert values["telegram.proxy_username"] == ""
    assert values["telegram.proxy_password"] == ""

    view.deleteLater()
    app.processEvents()
