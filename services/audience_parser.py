from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_SAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._-]+", re.UNICODE)


def classify_audience_user(user: Any) -> tuple[str, str | None]:
    """Return a stable filtering reason and normalized Telegram username."""

    if bool(getattr(user, "deleted", False)):
        return "deleted", None
    if bool(getattr(user, "bot", False)):
        return "bot", None
    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    if not username:
        return "missing_username", None
    return "accepted", f"@{username}"


def safe_filename_component(value: object, *, fallback: str = "group") -> str:
    clean = _SAFE_FILENAME_RE.sub("_", str(value or "").strip()).strip("._-")
    return clean[:80] or fallback


def build_audience_export_filename(
    title: object, *, when: datetime | None = None
) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H%M")
    return f"audience_{safe_filename_component(title)}_{stamp}.txt"


def validate_audience_task_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the queue payload before any Telegram work starts."""

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
    return result
