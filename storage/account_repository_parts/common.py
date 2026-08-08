from __future__ import annotations

import hashlib

import json

import re

from typing import TYPE_CHECKING, Any, cast

from core.account_limits import (
    MAX_REGISTERED_TELEGRAM_ACCOUNTS,
    account_limit_message,
)

from core.config import MAX_COMMENT_VARIANTS

from storage.db_common import DatabaseError

from storage.sqlcipher_driver import dbapi as sqlite3

MAX_TELEGRAM_ACCOUNTS = MAX_REGISTERED_TELEGRAM_ACCOUNTS

ACCOUNT_STATES = frozenset(
    {
        "disconnected",
        "connecting",
        "connected",
        "running",
        "paused",
        "stopping",
        "stopped",
        "network_wait",
        "flood_wait",
        "restricted",
        "authorization_required",
        "error",
    }
)

SESSION_NAME_RE = re.compile(r"^(?:main|account_[1-9][0-9]*|pending_[a-f0-9]{16,64})$")

ACCOUNT_SETTING_PREFIXES = (
    "telegram.",
    "automation.",
    "commenting.",
    "openai.",
    "scheduler.",
)

SECRET_ACCOUNT_SETTING_KEYS = frozenset(
    {
        "telegram.api_hash",
        "telegram.phone",
        "telegram.proxy_username",
        "telegram.proxy_password",
        "openai.api_key",
    }
)

def _positive_account_id(value: object) -> int:
    try:
        parsed: int = int(cast(Any, value) or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DatabaseError(f"Invalid Telegram account id: {value!r}") from exc
    if parsed <= 0:
        raise DatabaseError("Telegram account id must be positive")
    return parsed

def _mask_phone(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if not digits:
        return ""
    tail = digits[-4:]
    country = f"+{digits[0]}" if digits else "+"
    return f"{country} *** ***-{tail[:2]}-{tail[2:]}" if len(tail) == 4 else f"{country} ***"

def _normalized_slots(values: list[str] | tuple[str, ...] | None) -> list[str]:
    result = [str(item or "").strip() for item in list(values or [])[:MAX_COMMENT_VARIANTS]]
    result += [""] * (MAX_COMMENT_VARIANTS - len(result))
    return result

def _active_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result

def _fingerprint(values: list[str]) -> str:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
