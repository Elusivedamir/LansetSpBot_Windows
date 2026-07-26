from __future__ import annotations

import json
from datetime import date, datetime


def _telegram_id(value):
    """Return a JSON-safe scalar or the numeric ID of a Telethon object."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _telegram_id(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_telegram_id(v) for v in value]
    object_id = getattr(value, "id", None)
    if object_id is not None:
        try:
            return int(object_id)
        except (TypeError, ValueError):
            return str(object_id)
    for attr in ("user_id", "chat_id", "channel_id"):
        peer_id = getattr(value, attr, None)
        if peer_id is not None:
            return int(peer_id)
    raise TypeError(
        f"Unsupported non-serializable payload type: {type(value).__name__}"
    )


def json_dumps_safe(payload):
    return json.dumps(_telegram_id(payload), ensure_ascii=False)


class DatabaseError(Exception):
    """Database operation error."""


def resolve_account_id(database, account_id=None) -> int:
    """Return an explicit or currently selected Telegram account id.

    Account ``0`` is retained only for unauthenticated legacy/import/test data.
    Telegram workers reject it before any external action.
    """
    value = account_id
    if value is None:
        value = database.get_setting("telegram.account_id", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise DatabaseError(f"Invalid Telegram account id: {value!r}") from exc
