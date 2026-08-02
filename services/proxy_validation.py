"""Validation and normalization for user-provided proxy settings."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

SUPPORTED_PROXY_TYPES = frozenset({"SOCKS5", "SOCKS4", "HTTP"})
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class ProxyConfig:
    proxy_type: str
    host: str
    port: int
    username: str
    password: str


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


def normalize_proxy_config(
    proxy_type: object,
    host: object,
    port: object,
    username: object = "",
    password: object = "",
) -> ProxyConfig:
    normalized_type = str(proxy_type or "SOCKS5").strip().upper()
    if normalized_type not in SUPPORTED_PROXY_TYPES:
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
    if len(normalized_username) > 1024:
        raise ValueError("Логин proxy слишком длинный")
    if len(normalized_password) > 4096:
        raise ValueError("Пароль proxy слишком длинный")
    _reject_control_chars(normalized_username, "Логин")
    _reject_control_chars(normalized_password, "Пароль")

    return ProxyConfig(
        proxy_type=normalized_type,
        host=normalized_host,
        port=normalized_port,
        username=normalized_username,
        password=normalized_password,
    )
