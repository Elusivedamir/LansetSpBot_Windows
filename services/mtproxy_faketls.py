"""Telethon transport for Telegram MTProxy Fake TLS (``ee`` secrets).

Telethon 1.44 understands classic and randomized-intermediate MTProxy
connections, but deliberately removes the domain suffix from ``ee`` secrets.
A real Fake-TLS proxy expects a TLS-shaped ClientHello before the ordinary
randomized-intermediate MTProxy handshake, and wraps all following bytes in TLS
application-data records.

This module keeps that compatibility layer local to Marlen.  It has no optional
crypto dependency: Fake TLS authenticates its synthetic hello with HMAC-SHA256;
the actual MTProto stream encryption remains Telethon's existing MTProxyIO.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from typing import Any, cast

from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyRandomizedIntermediate,
)

from services.proxy_validation import MAX_FAKE_TLS_DOMAIN_BYTES

_TLS_VERSION = b"\x03\x03"
_TLS_CHANGE_CIPHER_SPEC = 0x14
_TLS_HANDSHAKE = 0x16
_TLS_APPLICATION_DATA = 0x17
_MAX_TLS_PLAINTEXT = 16 * 1024
_CLIENT_HELLO_SIZE = 517
_SERVER_HELLO_PREFIX_SIZE = 127 + 6 + 3 + 2


class FakeTLSProtocolError(ConnectionError):
    """The proxy did not complete the authenticated Fake-TLS handshake."""


def _hmac_sha256(key: bytes, message: bytes) -> bytes:
    return hmac.new(key=key, msg=message, digestmod=hashlib.sha256).digest()


def _split_ee_secret(secret: str) -> tuple[bytes, bytes]:
    """Return ``(16-byte key, SNI domain)`` from canonical full ee-hex."""

    value = str(secret or "").strip().lower()
    if not value.startswith("ee") or len(value) <= 34 or len(value) % 2:
        raise ValueError("Fake TLS требует полный Secret: ee + 16 байт + домен")
    try:
        payload = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("Некорректный hex Secret Fake TLS") from exc
    if payload[:1] != b"\xee" or len(payload) < 18:
        raise ValueError("Некорректный Secret Fake TLS")
    key = payload[1:17]
    domain = payload[17:]
    if len(key) != 16 or not domain:
        raise ValueError("В Secret Fake TLS отсутствует ключ или домен")
    return key, domain


class MTProxyFakeTLSClientHello:
    """Build and verify Telegram's authenticated TLS-shaped handshake."""

    def __init__(self, canonical_secret: str):
        self.secret, self.domain = _split_ee_secret(canonical_secret)
        self._session_id = b""
        self._client_digest = b""

    @staticmethod
    def _x25519_shaped_public_key() -> bytes:
        # Fake TLS does not negotiate real TLS keys.  Telegram clients still put
        # a syntactically plausible X25519 value into the key_share extension.
        prime = 2**255 - 19
        candidate = secrets.randbelow(prime)
        return pow(candidate, 2, prime).to_bytes(32, "little")

    def _fields(self) -> OrderedDict[str, bytes]:
        domain_length = len(self.domain)
        if not 1 <= domain_length <= MAX_FAKE_TLS_DOMAIN_BYTES:
            raise ValueError(
                "Домен Fake TLS должен содержать от 1 до "
                f"{MAX_FAKE_TLS_DOMAIN_BYTES} байт"
            )

        fields: OrderedDict[str, bytes] = OrderedDict(
            (
                ("content_type", b"\x16"),
                ("version", b"\x03\x01"),
                ("record_length", b"\x02\x00"),
                ("handshake_type", b"\x01"),
                ("handshake_length", b"\x00\x01\xfc"),
                ("handshake_version", b"\x03\x03"),
                ("random", b"\x00" * 32),
                ("session_id_length", b"\x20"),
                ("session_id", self._session_id),
                ("cipher_suites_length", b"\x00\x20"),
                (
                    "cipher_suites",
                    b"\xfa\xfa\x13\x01\x13\x02\x13\x03\xc0\x2b\xc0\x2f"
                    b"\xc0\x2c\xc0\x30\xcc\xa9\xcc\xa8\xc0\x13\xc0\x14"
                    b"\x00\x9c\x00\x9d\x00\x2f\x00\x35",
                ),
                ("compression_methods_length", b"\x01"),
                ("compression_methods", b"\x00"),
                ("extensions_length", b"\x01\x93"),
                ("grease_1", b"\x4a\x4a\x00\x00"),
                ("sni_type", b"\x00\x00"),
                ("sni_extension_length", (5 + domain_length).to_bytes(2, "big")),
                ("sni_list_length", (3 + domain_length).to_bytes(2, "big")),
                ("sni_name_type", b"\x00"),
                ("sni_name_length", domain_length.to_bytes(2, "big")),
                ("sni_name", self.domain),
                ("extended_master_secret", b"\x00\x17\x00\x00"),
                ("renegotiation_info", b"\xff\x01\x00\x01\x00"),
                (
                    "supported_groups",
                    b"\x00\x0a\x00\x0a\x00\x08\xba\xba\x00\x1d\x00\x17\x00\x18",
                ),
                ("ec_point_formats", b"\x00\x0b\x00\x02\x01\x00"),
                ("session_ticket", b"\x00\x23\x00\x00"),
                (
                    "alpn",
                    b"\x00\x10\x00\x0e\x00\x0c\x02h2\x08http/1.1",
                ),
                ("status_request", b"\x00\x05\x00\x05\x01\x00\x00\x00\x00"),
                (
                    "signature_algorithms",
                    b"\x00\x0d\x00\x12\x00\x10\x04\x03\x08\x04\x04\x01"
                    b"\x05\x03\x08\x05\x05\x01\x08\x06\x06\x01",
                ),
                ("signed_certificate_timestamp", b"\x00\x12\x00\x00"),
                ("key_share_type", b"\x00\x33"),
                ("key_share_length", b"\x00\x2b"),
                ("key_share_client_length", b"\x00\x29"),
                ("key_share_grease", b"\xba\xba\x00\x01\x00"),
                ("key_share_group", b"\x00\x1d"),
                ("key_share_exchange_length", b"\x00\x20"),
                ("key_share_exchange", self._x25519_shaped_public_key()),
                ("psk_key_exchange_modes", b"\x00\x2d\x00\x02\x01\x01"),
                (
                    "supported_versions",
                    b"\x00\x2b\x00\x0b\x0a\x9a\x9a\x03\x04\x03\x03\x03\x02\x03\x01",
                ),
                ("compress_certificate", b"\x00\x1b\x00\x03\x02\x00\x02"),
                ("grease_2", b"\x1a\x1a\x00\x01\x00"),
                ("padding_type", b"\x00\x15"),
                ("padding_length", b"\x00\x00"),
                ("padding", b""),
            )
        )
        return fields

    def build(self) -> bytes:
        self._session_id = secrets.token_bytes(32)
        fields = self._fields()

        packet_without_padding = b"".join(fields.values())
        padding_length = _CLIENT_HELLO_SIZE - len(packet_without_padding)
        if padding_length < 0 or padding_length > 0xFFFF:
            raise ValueError("Домен Fake TLS не помещается в ClientHello")
        fields["padding_length"] = padding_length.to_bytes(2, "big")
        fields["padding"] = b"\x00" * padding_length

        fields["random"] = b"\x00" * 32
        unsigned_packet = b"".join(fields.values())
        digest = _hmac_sha256(self.secret, unsigned_packet)
        timestamp = int(time.time()).to_bytes(4, "little", signed=False)
        authenticated_random = digest[:28] + bytes(
            timestamp[index] ^ digest[28 + index] for index in range(4)
        )
        fields["random"] = authenticated_random
        packet = b"".join(fields.values())

        if len(packet) != _CLIENT_HELLO_SIZE:
            raise AssertionError(
                f"Fake TLS ClientHello has invalid size: {len(packet)}"
            )
        self._client_digest = authenticated_random
        return packet

    def verify_server_hello(self, server_hello: bytes) -> None:
        if len(server_hello) < 136:
            raise FakeTLSProtocolError("Fake TLS: слишком короткий ServerHello")
        if not server_hello.startswith(b"\x16\x03\x03"):
            raise FakeTLSProtocolError("Fake TLS: сервер вернул не TLS ServerHello")
        if server_hello[127:136] != b"\x14\x03\x03\x00\x01\x01\x17\x03\x03":
            raise FakeTLSProtocolError("Fake TLS: некорректная структура ServerHello")
        session_id = server_hello[44:76]
        if not hmac.compare_digest(session_id, self._session_id):
            raise FakeTLSProtocolError("Fake TLS: сервер не подтвердил session id")

        server_digest = server_hello[11:43]
        unsigned_server_hello = (
            server_hello[:11] + b"\x00" * 32 + server_hello[43:]
        )
        expected_digest = _hmac_sha256(
            self.secret, self._client_digest + unsigned_server_hello
        )
        if not hmac.compare_digest(server_digest, expected_digest):
            raise FakeTLSProtocolError("Fake TLS: неверная подпись ServerHello")


class FakeTLSStreamReader:
    """Unwrap TLS application-data records into Telethon's byte stream."""

    def __init__(self, upstream: asyncio.StreamReader):
        self._upstream = upstream
        self._buffer = bytearray()

    async def _read_record(self) -> bytes:
        while True:
            record_type = (await self._upstream.readexactly(1))[0]
            version = await self._upstream.readexactly(2)
            if version != _TLS_VERSION:
                raise FakeTLSProtocolError("Fake TLS: неизвестная версия TLS record")
            length = int.from_bytes(await self._upstream.readexactly(2), "big")
            if length > _MAX_TLS_PLAINTEXT + 2048:
                raise FakeTLSProtocolError("Fake TLS: слишком большой TLS record")
            payload = await self._upstream.readexactly(length)
            if not payload and record_type == _TLS_APPLICATION_DATA:
                continue
            if record_type == _TLS_CHANGE_CIPHER_SPEC:
                continue
            if record_type != _TLS_APPLICATION_DATA:
                raise FakeTLSProtocolError("Fake TLS: неожиданный тип TLS record")
            return payload

    async def read(self, n: int = -1) -> bytes:
        if n == 0:
            return b""
        if n < 0:
            if not self._buffer:
                self._buffer.extend(await self._read_record())
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        while len(self._buffer) < n:
            self._buffer.extend(await self._read_record())
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    async def readexactly(self, n: int) -> bytes:
        if n < 0:
            raise ValueError("readexactly size must be non-negative")
        while len(self._buffer) < n:
            self._buffer.extend(await self._read_record())
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def at_eof(self) -> bool:
        return not self._buffer and self._upstream.at_eof()


class FakeTLSStreamWriter:
    """Wrap Telethon writes in TLS 1.3 application-data records."""

    def __init__(self, upstream: asyncio.StreamWriter):
        self._upstream = upstream

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        for offset in range(0, len(view), _MAX_TLS_PLAINTEXT):
            chunk = view[offset : offset + _MAX_TLS_PLAINTEXT]
            self._upstream.write(
                bytes((_TLS_APPLICATION_DATA,))
                + _TLS_VERSION
                + len(chunk).to_bytes(2, "big")
                + bytes(chunk)
            )
        return len(data)

    def write_eof(self) -> Any:
        return self._upstream.write_eof()

    async def drain(self) -> None:
        await self._upstream.drain()

    def close(self) -> None:
        self._upstream.close()

    async def wait_closed(self) -> None:
        await self._upstream.wait_closed()

    def is_closing(self) -> bool:
        return self._upstream.is_closing()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._upstream.get_extra_info(name, default)

    @property
    def transport(self) -> Any:
        return self._upstream.transport


async def _read_fake_tls_server_hello(reader: asyncio.StreamReader) -> bytes:
    prefix = await reader.readexactly(_SERVER_HELLO_PREFIX_SIZE)
    application_length = int.from_bytes(prefix[-2:], "big")
    if application_length > _MAX_TLS_PLAINTEXT + 2048:
        raise FakeTLSProtocolError("Fake TLS: слишком большой ServerHello record")
    return prefix + await reader.readexactly(application_length)


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):
    """Randomized-intermediate MTProxy wrapped in Telegram Fake TLS."""

    def __init__(
        self,
        ip: str,
        port: int,
        dc_id: int,
        *,
        loggers: dict[str, Any],
        proxy: tuple[str, int, str] | None = None,
        local_addr: str | tuple[str, int] | None = None,
    ):
        if proxy is None or len(proxy) < 3:
            raise ValueError("No proxy info specified for MTProxy Fake TLS")
        key, _domain = _split_ee_secret(proxy[2])
        self._fake_tls_hello = MTProxyFakeTLSClientHello(proxy[2])
        # Telethon's ordinary MTProxy layer must see only the 16-byte key.  The
        # full ee secret remains owned by the Fake-TLS layer above it.
        super().__init__(
            ip,
            port,
            dc_id,
            loggers=loggers,
            proxy=(proxy[0], proxy[1], key.hex()),
            local_addr=local_addr,
        )
        # Telethon 1.44's TcpMTProxy currently drops local_addr while replacing
        # the Telegram DC address with the MTProxy address. Preserve it here.
        self._local_addr = local_addr

    async def _connect(self, timeout: float | None = None, ssl: Any = None) -> None:
        if ssl is not None:
            raise ValueError("Fake TLS MTProxy cannot be combined with real SSL")
        local_addr: tuple[str, int] | None
        if self._local_addr is None:
            local_addr = None
        elif isinstance(self._local_addr, tuple) and len(self._local_addr) == 2:
            local_addr = self._local_addr
        elif isinstance(self._local_addr, str):
            local_addr = (self._local_addr, 0)
        else:
            raise ValueError(f"Unknown local address format: {self._local_addr!r}")

        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=self._ip,
                    port=self._port,
                    local_addr=local_addr,
                ),
                timeout=timeout,
            )
            # asyncio.open_connection() always yields both handles; the
            # Optional annotations exist only for the except/finally cleanup.
            open_reader = cast(asyncio.StreamReader, reader)
            open_writer = cast(asyncio.StreamWriter, writer)
            open_writer.write(self._fake_tls_hello.build())
            await open_writer.drain()
            server_hello_awaitable = _read_fake_tls_server_hello(open_reader)
            server_hello = (
                await asyncio.wait_for(server_hello_awaitable, timeout=timeout)
                if timeout is not None
                else await server_hello_awaitable
            )
            self._fake_tls_hello.verify_server_hello(server_hello)

            self._reader = FakeTLSStreamReader(open_reader)
            self._writer = FakeTLSStreamWriter(open_writer)
            self._codec = self.packet_codec(self)
            self._init_conn()
            await self._writer.drain()
        except asyncio.IncompleteReadError as exc:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
                except Exception:
                    pass
            raise FakeTLSProtocolError(
                "MTProxy закрыл соединение во время Fake-TLS handshake. "
                "Проверьте, что Secret содержит EE-домен и сам proxy доступен."
            ) from exc
        except Exception:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
                except Exception:
                    pass
            raise


__all__ = [
    "ConnectionTcpMTProxyFakeTLS",
    "FakeTLSProtocolError",
    "FakeTLSStreamReader",
    "FakeTLSStreamWriter",
    "MTProxyFakeTLSClientHello",
]
