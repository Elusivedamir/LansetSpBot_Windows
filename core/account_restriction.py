from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core.redaction import sanitize_data, sanitize_text

RESTRICTION_CODES = frozenset(
    {
        "user_banned",
        "peer_flood",
        "user_restricted",
        "auth_key_duplicated",
    }
)

RESTRICTION_REASON = (
    "Telegram ограничил активность аккаунта. Кампании этого аккаунта "
    "остановлены; проверьте ограничения в официальном приложении Telegram."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_account_id(database: Any, account_id: Any = None) -> int:
    value = account_id
    if value is None:
        value = database.get_setting("telegram.account_id", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid Telegram account id: {value!r}") from exc


def get_account_restriction_state(
    database: Any, *, account_id: Any = None
) -> dict[str, Any]:
    """Return the restriction row for exactly one Telegram account."""

    owner_account_id = _resolve_account_id(database, account_id)
    getter = getattr(database, "get_account_restriction", None)
    if not callable(getter):
        raise RuntimeError("Database does not support account-scoped restrictions")
    return dict(getter(owner_account_id) or {})


def build_account_restriction_kwargs(
    database: Any,
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    account_id: Any = None,
) -> dict[str, Any]:
    """Normalize one critical Telegram restriction for repository methods."""

    normalized_code = str(code or "").strip().lower()
    if normalized_code not in RESTRICTION_CODES:
        raise ValueError(f"Unsupported account restriction code: {code}")

    owner_account_id = _resolve_account_id(database, account_id)
    payload = sanitize_data(dict(details or {}))
    safe_message = sanitize_text(message or RESTRICTION_REASON)
    return {
        "account_id": owner_account_id,
        "code": normalized_code,
        "message": safe_message,
        "details_json": json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=sanitize_text
        ),
        "detected_at": _now_iso(),
        "reason": RESTRICTION_REASON,
    }


def activate_account_restriction(
    database: Any,
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    account_id: Any = None,
) -> dict[str, Any]:
    """Atomically restrict only the affected account and stop its mutations."""

    restriction_kwargs = build_account_restriction_kwargs(
        database,
        code=code,
        message=message,
        details=details,
        account_id=account_id,
    )
    activator = getattr(database, "activate_account_restriction_atomic", None)
    if not callable(activator):
        raise RuntimeError("Database does not support atomic account restrictions")
    return dict(activator(**restriction_kwargs) or {})


def clear_account_restriction_after_authoritative_check(
    database: Any, *, account_id: Any = None
) -> dict[str, Any]:
    """Clear a stored restriction after an external authoritative verification.

    This low-level operation is intentionally not exposed as a GUI button. It is
    retained for controlled recovery/migration code that has independently
    verified Telegram-side state.
    """

    owner_account_id = _resolve_account_id(database, account_id)
    state = get_account_restriction_state(database, account_id=owner_account_id)
    checked_at = _now_iso()
    clearer = getattr(database, "clear_account_restriction", None)
    if not callable(clearer):
        raise RuntimeError("Database does not support account-scoped restrictions")
    clearer(account_id=owner_account_id, checked_at=checked_at)
    state["active"] = False
    state["stored_active"] = False
    state["checked_at"] = checked_at
    return state
