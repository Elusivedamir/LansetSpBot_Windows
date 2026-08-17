from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

_SAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._-]+", re.UNICODE)
_ALLOWED_ACTIVITY_DAYS = {0, 7, 30, 90}


def normalize_audience_filters(value: object) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    try:
        activity_days = int(raw.get("activity_days") or 0)
    except (TypeError, ValueError, OverflowError):
        activity_days = 0
    if activity_days not in _ALLOWED_ACTIVITY_DAYS:
        activity_days = 0
    return {
        "exclude_admins": bool(raw.get("exclude_admins", False)),
        "exclude_scam_fake": bool(raw.get("exclude_scam_fake", False)),
        "activity_days": activity_days,
    }


def _activity_is_recent(user: Any, days: int, *, now: datetime | None = None) -> bool:
    if days <= 0:
        return True
    status = getattr(user, "status", None)
    status_name = type(status).__name__.casefold()
    if "online" in status_name or "recently" in status_name:
        return True
    if "lastweek" in status_name:
        # Telegram's "last week" status means the user was online within the
        # last seven days, so any 7-day-or-wider window must accept them.
        return days >= 7
    if "lastmonth" in status_name:
        return days >= 30
    was_online = getattr(status, "was_online", None)
    if isinstance(was_online, datetime):
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        if was_online.tzinfo is None:
            was_online = was_online.replace(tzinfo=timezone.utc)
        return was_online >= reference.astimezone(timezone.utc) - timedelta(days=days)
    return False


def classify_audience_user(
    user: Any,
    *,
    filters: Mapping[str, Any] | None = None,
    is_administrator: bool = False,
    administrator_ids: set[int] | None = None,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    if bool(getattr(user, "deleted", False)):
        return "deleted", None
    if bool(getattr(user, "bot", False)):
        return "bot", None

    normalized = normalize_audience_filters(filters)
    try:
        user_id = int(getattr(user, "id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        user_id = 0
    administrator = bool(is_administrator) or user_id in set(administrator_ids or set())
    if normalized["exclude_admins"] and administrator:
        return "administrator", None
    if normalized["exclude_scam_fake"] and (
        bool(getattr(user, "scam", False)) or bool(getattr(user, "fake", False))
    ):
        return "scam_fake", None
    if not _activity_is_recent(user, int(normalized["activity_days"]), now=now):
        return "inactive", None

    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    if not username:
        return "missing_username", None
    return "accepted", f"@{username}"


def safe_filename_component(value: object, *, fallback: str = "group") -> str:
    clean = _SAFE_FILENAME_RE.sub("_", str(value or "").strip()).strip("._-")
    return clean[:80] or fallback


def build_audience_export_filename(title: object, *, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H%M")
    return f"audience_{safe_filename_component(title)}_{stamp}.txt"


def validate_audience_task_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Параметры парсинга должны быть объектом")
    result = dict(payload)
    source = result.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Выберите одну группу для парсинга")

    normalized_source = dict(source)
    link = str(normalized_source.get("link") or "").strip()
    raw_peer_id = normalized_source.get("peer_id")
    peer_id = 0
    if raw_peer_id not in (None, ""):
        try:
            peer_id = int(raw_peer_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Некорректный ID группы") from exc
    if bool(link) == bool(peer_id):
        raise ValueError("Укажите ссылку либо выберите одну группу из списка")

    if link:
        normalized_source = {"link": link}
    else:
        peer_type = str(normalized_source.get("peer_type") or "").strip().lower()
        if peer_type not in {"chat", "channel"}:
            raise ValueError("Не удалось определить тип выбранной группы")
        normalized_source = {
            "peer_id": peer_id,
            "peer_type": peer_type,
            "title": str(normalized_source.get("title") or "").strip(),
        }
        raw_access_hash = source.get("access_hash")
        if raw_access_hash not in (None, ""):
            try:
                normalized_source["access_hash"] = int(raw_access_hash)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Некорректные данные доступа к группе") from exc

    raw_output_path = str(result.get("output_path") or "").strip()
    if not raw_output_path:
        raise ValueError("Выберите TXT-файл для экспорта")
    output_path = Path(raw_output_path).expanduser()
    if output_path.suffix.lower() != ".txt":
        raise ValueError("Экспорт аудитории поддерживает только TXT-файлы")

    result["source"] = normalized_source
    result["output_path"] = str(output_path)
    result["source_title"] = str(result.get("source_title") or "").strip()
    result["filters"] = normalize_audience_filters(result.get("filters"))
    checkpoint = result.get("_audience_checkpoint")
    if isinstance(checkpoint, Mapping) and checkpoint:
        result["_audience_checkpoint"] = dict(checkpoint)
    else:
        result.pop("_audience_checkpoint", None)
    return result
