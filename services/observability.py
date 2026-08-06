from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

_ACTIVE_TASK_STATUSES = {"pending", "running", "processing", "paused"}
_REASON_EXACT = {
    "pending": "Ожидает выполнения",
    "running": "Выполняется",
    "processing": "Обрабатывается",
    "paused": "Приостановлено",
    "completed": "Завершено",
    "failed": "Ошибка выполнения",
    "cancelled": "Отменено пользователем",
    "stopped": "Остановлено пользователем",
    "network_wait": "Ожидание восстановления сети",
    "cycle_wait": "Суточный цикл выполнен",
    "uncertain": "Отправка не подтверждена Telegram",
    "sent": "Комментарий подтверждён и отправлен",
    "skipped": "Пропущено по безопасному правилу",
    "flood_wait": "Telegram временно ограничил частоту запросов",
    "flood_wait_deferred": "Ожидание окончания FloodWait Telegram",
    "authorization_required": "Требуется повторная авторизация аккаунта",
    "account_restricted": "Telegram ограничил активность аккаунта",
    "proxy_error": "Прокси недоступен",
    "audience_members_hidden": "Telegram скрыл список участников группы",
    "audience_membership_required": "Аккаунт не состоит в выбранной группе",
    "audience_group_inaccessible": "Группа недоступна выбранному аккаунту",
}


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def parse_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    result = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def format_utc_datetime(value: object, *, fallback: str = "—") -> str:
    parsed = parse_utc_datetime(value)
    if parsed is None:
        return fallback
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def humanize_reason(value: object, *, fallback: str = "—") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    normalized = text.casefold().replace("-", "_").replace(" ", "_")
    if normalized in _REASON_EXACT:
        return _REASON_EXACT[normalized]
    if len(text.split()) >= 3 or any(char in text for char in ".:;·"):
        return text
    return _REASON_EXACT.get(normalized, text)


def classify_result(value: object) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return "other"
    if any(token in text for token in ("uncertain", "не подтверж", "неопредел", "unknown")):
        return "uncertain"
    if any(token in text for token in ("cancelled", "canceled", "отмен", "останов")):
        return "cancelled"
    if any(token in text for token in ("skipped", "пропущ", "уже обработ", "нет обсужден", "комментарии отключ")):
        return "skipped"
    if any(
        token in text
        for token in (
            "failed",
            "error",
            "ошиб",
            "огранич",
            "restricted",
            "недоступ",
            "proxy",
            "прокси",
            "flood",
            "timeout",
            "не удалось",
        )
    ):
        return "failed"
    if any(token in text for token in ("sent", "success", "успеш", "отправлен", "готово")):
        return "success"
    return "other"


def campaign_statistics(
    state: Mapping[str, Any] | None,
    history: Iterable[Mapping[str, Any]] | None,
) -> dict[str, int]:
    campaign = dict(state or {})
    rows = [dict(row) for row in (history or [])]
    buckets = Counter(classify_result(row.get("status")) for row in rows)
    planned = max(0, int(campaign.get("planned_count") or campaign.get("daily_limit") or 0))
    attempted = max(int(campaign.get("attempted_count") or 0), len(rows))
    sent = max(int(campaign.get("sent_count") or 0), buckets["success"])
    return {
        "planned": planned,
        "attempted": attempted,
        "sent": sent,
        "skipped": buckets["skipped"],
        "failed": buckets["failed"],
        "cancelled": buckets["cancelled"],
        "uncertain": buckets["uncertain"],
        "remaining": max(0, planned - attempted),
    }


def format_campaign_statistics(stats: Mapping[str, Any]) -> str:
    return (
        f"Запланировано: {int(stats.get('planned') or 0)} · "
        f"Попыток: {int(stats.get('attempted') or 0)} · "
        f"Успешно: {int(stats.get('sent') or 0)} · "
        f"Пропущено: {int(stats.get('skipped') or 0)} · "
        f"Ошибок: {int(stats.get('failed') or 0)} · "
        f"Отменено: {int(stats.get('cancelled') or 0)} · "
        f"Не подтверждено: {int(stats.get('uncertain') or 0)} · "
        f"Осталось: {int(stats.get('remaining') or 0)}"
    )


def _call(database: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(database, name, None)
    if not callable(method):
        return None
    return method(*args, **kwargs)


def _account_row(database: Any, account_id: int) -> dict[str, Any]:
    rows = _call(database, "list_telegram_accounts") or []
    for value in rows:
        row = _mapping(value)
        try:
            row_id = int(row.get("telegram_account_id") or row.get("id") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if row_id == account_id:
            return row
    return {}


def _history_timestamp(row: Mapping[str, Any]) -> datetime | None:
    for key in ("sent_at", "created_at", "updated_at", "finished_at"):
        parsed = parse_utc_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _active_cooldown(database: Any, account_id: int, *, now: datetime) -> dict[str, Any]:
    try:
        raw = _call(database, "get_account_rpc_cooldown", account_id=account_id)
    except Exception:
        return {}
    cooldown = _mapping(raw)
    deadline = None
    for key in (
        "effective_next_allowed_at",
        "next_allowed_at",
        "retry_at",
        "not_before",
    ):
        deadline = parse_utc_datetime(cooldown.get(key))
        if deadline is not None:
            break
    try:
        remaining = max(0, int(cooldown.get("remaining_seconds") or 0))
    except (TypeError, ValueError, OverflowError):
        remaining = 0
    active = bool(cooldown.get("active")) or remaining > 0 or bool(deadline and deadline > now)
    if not active:
        return {}
    result = dict(cooldown)
    result["deadline"] = deadline
    result["remaining_seconds"] = remaining
    return result


def build_account_health_snapshot(
    database: Any,
    account_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    owner = max(0, int(account_id or 0))
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if owner <= 0:
        return {
            "account_id": 0,
            "status": "Аккаунт не выбран",
            "proxy": "—",
            "current_task": "—",
            "sent_24h": 0,
            "errors_24h": 0,
            "flood_wait": "—",
            "last_success": "—",
            "last_error": "—",
        }

    account = _account_row(database, owner)
    runtime_state = str(account.get("runtime_state") or account.get("state") or "").casefold()
    stopped = bool(account.get("stopped")) or runtime_state in {"stopping", "stopped"}
    authorized = account.get("authorized")
    restriction = _mapping(_call(database, "get_account_restriction_state", account_id=owner) or {})
    if bool(restriction.get("active")) or runtime_state == "restricted":
        status = "Ограничен Telegram"
    elif authorized is False or runtime_state == "authorization_required":
        status = "Требуется авторизация"
    elif stopped:
        status = "Остановлен"
    elif runtime_state == "error":
        status = "Ошибка"
    else:
        status = "Активен"

    settings = _mapping(_call(database, "get_account_settings", owner) or {})
    proxy_enabled = str(settings.get("telegram.proxy_enabled") or "0").strip().casefold() in {
        "1", "true", "yes", "on"
    }
    proxy_host = str(settings.get("telegram.proxy_host") or "").strip()
    proxy = (
        f"Подключён · {proxy_host}"
        if proxy_enabled and proxy_host
        else "Подключён"
        if proxy_enabled
        else "Не используется"
    )

    tasks = [_mapping(row) for row in (_call(database, "get_tasks") or [])]
    active_tasks = []
    for task in tasks:
        try:
            task_owner = int(task.get("account_id") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if task_owner == owner and str(task.get("status") or "") in _ACTIVE_TASK_STATUSES:
            active_tasks.append(task)
    active_tasks.sort(
        key=lambda row: (
            0 if str(row.get("status") or "") in {"running", "processing"} else 1,
            -int(row.get("id") or 0),
        )
    )
    if active_tasks:
        task = active_tasks[0]
        current_task = (
            f"{task.get('type') or 'задача'} · "
            f"{humanize_reason(task.get('status_text') or task.get('error') or task.get('status'))}"
        )
    else:
        current_task = "—"

    history = [
        _mapping(row)
        for row in (
            _call(database, "get_comment_history", limit=1000, account_id=owner) or []
        )
    ]
    history.sort(key=lambda row: _history_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    cutoff = reference - timedelta(hours=24)
    recent = [row for row in history if (_history_timestamp(row) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    recent_classes = [classify_result(row.get("status")) for row in recent]
    sent_24h = recent_classes.count("success")
    errors_24h = recent_classes.count("failed") + recent_classes.count("uncertain")

    last_success_row = next(
        (row for row in history if classify_result(row.get("status")) == "success"),
        None,
    )
    last_error_row = next(
        (row for row in history if classify_result(row.get("status")) in {"failed", "uncertain"}),
        None,
    )
    last_success = format_utc_datetime(_history_timestamp(last_success_row or {}))
    last_error_value = (
        (last_error_row or {}).get("status")
        or account.get("last_error")
        or restriction.get("reason")
        or "—"
    )

    cooldown = _active_cooldown(database, owner, now=reference)
    if cooldown:
        deadline = cooldown.get("deadline")
        code = humanize_reason(cooldown.get("code"), fallback="FloodWait")
        flood_wait = (
            f"{code} · до {format_utc_datetime(deadline)}"
            if deadline is not None
            else code
        )
    else:
        flood_wait = "Нет"

    return {
        "account_id": owner,
        "status": status,
        "proxy": proxy,
        "current_task": current_task,
        "sent_24h": sent_24h,
        "errors_24h": errors_24h,
        "flood_wait": flood_wait,
        "last_success": last_success,
        "last_error": humanize_reason(last_error_value),
        "updated_at": reference.isoformat(timespec="seconds"),
    }
