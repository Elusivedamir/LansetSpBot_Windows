"""Validation and normalization for user-provided proxy settings."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from dataclasses import dataclass

_ALLOWED_TYPES = {"SOCKS5", "SOCKS4", "HTTP", "MTPROXY"}
_TYPE_ALIASES = {"MTPROTO": "MTPROXY", "MT-PROXY": "MTPROXY", "MT PROXY": "MTPROXY"}
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_HEX_SECRET = re.compile(r"^[0-9a-fA-F]+$")

# Telegram's Fake-TLS ClientHello is exactly 517 bytes and every field except
# the SNI hostname and the trailing padding has a fixed size, which leaves room
# for at most 220 domain bytes. Accepting the DNS-level 253-byte limit here only
# moved the failure to connect time, where it surfaced as a raw ValueError from
# the transport instead of an actionable message next to the Secret field.
MAX_FAKE_TLS_DOMAIN_BYTES = 220


@dataclass(frozen=True)
class ProxyConfig:
    proxy_type: str
    host: str
    port: int
    username: str
    password: str
    secret: str


def _reject_control_chars(value: str, field: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} proxy содержит управляющие символы")


def _normalize_host(raw_host: object) -> str:
    host = str(raw_host or "").strip()
    if not host:
        raise ValueError("Для proxy укажите адрес")
    _reject_control_chars(host, "Адрес")
    if len(host) > 255:
        raise ValueError("Адрес proxy слишком длинный")
    if any(char.isspace() for char in host):
        raise ValueError("Адрес proxy не должен содержать пробелы")
    if "://" in host or any(char in host for char in "/\\@?#"):
        raise ValueError("Укажите только hostname или IP без URL, логина и пути")

    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if not candidate:
        raise ValueError("Адрес proxy пуст")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass

    if ":" in candidate:
        raise ValueError("Некорректный IPv6-адрес proxy")
    try:
        ascii_host = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Некорректное Unicode-имя proxy") from exc
    if len(ascii_host) > 253:
        raise ValueError("Hostname proxy слишком длинный")
    labels = ascii_host.rstrip(".").split(".")
    if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("Некорректный hostname proxy")
    return ascii_host.rstrip(".")


def normalize_mtproxy_secret(raw_secret: object) -> str:
    """Return a canonical MTProxy secret understood by Marlen transports.

    Classic and ``dd`` secrets are normalized to the 16-byte core key in
    lowercase hexadecimal form.  Fake-TLS ``ee`` secrets retain their marker
    and domain suffix as ``ee + key + domain_hex`` so the transport can perform
    the required TLS-shaped handshake instead of silently downgrading it to a
    plain randomized-intermediate connection.
    """

    secret = str(raw_secret or "").strip()
    if not secret:
        raise ValueError("Для MTProxy укажите Secret")
    _reject_control_chars(secret, "Secret")
    if any(char.isspace() for char in secret):
        raise ValueError("Secret MTProxy не должен содержать пробелы")
    if len(secret) > 1024:
        raise ValueError("Secret MTProxy слишком длинный")
    if "://" in secret or any(char in secret for char in "?&"):
        raise ValueError("Вставьте только Secret, а не ссылку tg://proxy")

    # A Base64URL secret can consist purely of hex characters. Classifying by
    # alphabet alone then rejected a perfectly valid link, so try the hex
    # interpretation first and fall back to Base64URL before giving up.
    payload: bytes | None = None
    hex_error: str | None = None
    if _HEX_SECRET.fullmatch(secret):
        if len(secret) < 32 or len(secret) % 2 != 0:
            hex_error = "Hex Secret MTProxy должен содержать не менее 32 символов"
        else:
            try:
                payload = bytes.fromhex(secret)
            except ValueError:  # defensive after regex/even-length checks
                hex_error = "Некорректный hex Secret MTProxy"
    if payload is None:
        padded = secret + "=" * (-len(secret) % 4)
        try:
            payload = base64.b64decode(
                padded.encode("ascii"), altchars=b"-_", validate=True
            )
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError(
                hex_error or "Secret MTProxy должен быть hex или Base64URL"
            ) from exc

    # A 16-byte value may naturally start with dd/ee and is still a classic
    # secret. Transport markers are recognized only when extra metadata exists.
    if len(payload) == 16:
        return payload.hex()

    if payload[:1] == b"\xdd":
        if len(payload) != 17:
            raise ValueError("DD Secret MTProxy должен содержать ровно 16 байт ключа")
        return payload[1:].hex()

    if payload[:1] == b"\xee":
        if len(payload) <= 17:
            raise ValueError("В EE Secret MTProxy отсутствует домен Fake TLS")
        core = payload[1:17]
        domain = payload[17:]
        try:
            domain_text = domain.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Домен Fake TLS должен быть ASCII/IDNA") from exc
        if not 1 <= len(domain) <= MAX_FAKE_TLS_DOMAIN_BYTES:
            raise ValueError(
                "Домен Fake TLS должен содержать от 1 до "
                f"{MAX_FAKE_TLS_DOMAIN_BYTES} байт"
            )
        if any(ord(char) < 33 or ord(char) == 127 for char in domain_text):
            raise ValueError("Домен Fake TLS содержит недопустимые символы")
        if any(char in domain_text for char in "/\\@?#:"):
            raise ValueError("В EE Secret указан некорректный домен Fake TLS")
        labels = domain_text.rstrip(".").split(".")
        if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise ValueError("В EE Secret указан некорректный домен Fake TLS")
        # Preserve the exact domain bytes from the Telegram link. They take part
        # in the authenticated ClientHello and should not be silently rewritten.
        return (b"\xee" + core + domain).hex()

    if len(payload) < 16:
        raise ValueError("Secret MTProxy должен содержать не менее 16 байт")
    if len(payload) > 16:
        raise ValueError(
            "Secret MTProxy длиннее 16 байт, но не содержит DD/EE-префикс"
        )
    return payload.hex()


def normalize_proxy_config(
    proxy_type: object,
    host: object,
    port: object,
    username: object = "",
    password: object = "",
    secret: object = "",
) -> ProxyConfig:
    normalized_type = str(proxy_type or "SOCKS5").strip().upper()
    normalized_type = _TYPE_ALIASES.get(normalized_type, normalized_type)
    if normalized_type not in _ALLOWED_TYPES:
        raise ValueError(f"Неподдерживаемый тип proxy: {normalized_type}")

    normalized_host = _normalize_host(host)
    try:
        normalized_port = int(str(port).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Порт proxy должен быть числом") from exc
    if not 1 <= normalized_port <= 65535:
        raise ValueError("Порт proxy должен быть от 1 до 65535")

    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    normalized_secret = str(secret or "").strip()

    if normalized_type == "MTPROXY":
        normalized_secret = normalize_mtproxy_secret(normalized_secret)
        # MTProxy authenticates with Secret, never with a SOCKS/HTTP login pair.
        normalized_username = ""
        normalized_password = ""
    else:
        if len(normalized_username) > 1024:
            raise ValueError("Логин proxy слишком длинный")
        if len(normalized_password) > 4096:
            raise ValueError("Пароль proxy слишком длинный")
        _reject_control_chars(normalized_username, "Логин")
        _reject_control_chars(normalized_password, "Пароль")
        normalized_secret = ""

    return ProxyConfig(
        proxy_type=normalized_type,
        host=normalized_host,
        port=normalized_port,
        username=normalized_username,
        password=normalized_password,
        secret=normalized_secret,
    )
