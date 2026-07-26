from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.exceptions import DeferredTelegramError

log = logging.getLogger(__name__)

SCHEDULE_ENABLED_KEY = "automation.schedule_enabled"
TIMEZONE_KEY = "automation.timezone"
QUIET_START_KEY = "automation.quiet_start"
QUIET_END_KEY = "automation.quiet_end"

DEFAULT_TIMEZONE = "UTC"
DEFAULT_QUIET_START = "22:00"
DEFAULT_QUIET_END = "07:00"


def normalize_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return bool(default)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Значение расписания должно быть 0/1 или true/false")


def validate_timezone_name(value: Any) -> str:
    name = str(value or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    if len(name) > 128 or any(char in name for char in ("\x00", "\r", "\n")):
        raise ValueError("Некорректное имя часового пояса")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            "Неизвестный часовой пояс. Используйте имя IANA, например Europe/Berlin"
        ) from exc
    return name


def parse_clock(value: Any, *, default: str) -> time:
    text = str(value or default).strip()
    try:
        parsed = datetime.strptime(text, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Время должно быть записано в формате ЧЧ:ММ") from exc
    return parsed.replace(second=0, microsecond=0)


def format_clock(value: time) -> str:
    return value.strftime("%H:%M")


@dataclass(frozen=True)
class ActivityScheduleConfig:
    """User-configured local quiet hours for automated comment dispatch.

    This policy is intentionally deterministic. It is an operational safety
    window, not a mechanism for imitating a person or bypassing Telegram's abuse
    controls. Telegram FloodWait/restriction handling and Marlen's durable
    dispatch barriers remain authoritative.
    """

    enabled: bool = False
    timezone_name: str = DEFAULT_TIMEZONE
    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "ActivityScheduleConfig":
        source = values or {}
        return cls(
            enabled=normalize_bool(source.get(SCHEDULE_ENABLED_KEY), default=False),
            timezone_name=validate_timezone_name(source.get(TIMEZONE_KEY)),
            quiet_start=parse_clock(
                source.get(QUIET_START_KEY), default=DEFAULT_QUIET_START
            ),
            quiet_end=parse_clock(source.get(QUIET_END_KEY), default=DEFAULT_QUIET_END),
        )

    def to_settings(self) -> dict[str, str]:
        return {
            SCHEDULE_ENABLED_KEY: "1" if self.enabled else "0",
            TIMEZONE_KEY: self.timezone_name,
            QUIET_START_KEY: format_clock(self.quiet_start),
            QUIET_END_KEY: format_clock(self.quiet_end),
        }


@dataclass(frozen=True)
class ScheduleDecision:
    allowed: bool
    local_now: datetime
    resume_at_local: datetime | None = None
    retry_after_seconds: int = 0


class ActivityScheduleManager:
    """Timezone-aware, cancellation-friendly activity-window enforcement."""

    def __init__(
        self,
        config: ActivityScheduleConfig,
        *,
        account_id: int | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.account_id = int(account_id or 0)
        self.timezone = ZoneInfo(config.timezone_name)
        self.logger = logger or log

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None,
        *,
        account_id: int | None = None,
        logger: logging.Logger | None = None,
    ) -> "ActivityScheduleManager":
        return cls(
            ActivityScheduleConfig.from_mapping(values),
            account_id=account_id,
            logger=logger,
        )

    def local_now(self, now: datetime | None = None) -> datetime:
        current = now or datetime.now(self.timezone)
        if current.tzinfo is None:
            return current.replace(tzinfo=self.timezone)
        return current.astimezone(self.timezone)


    def _resolve_local_deadline(
        self,
        target_date,
        target_time: time,
        *,
        after: datetime,
    ) -> datetime:
        """Return the first real local instant at or after a wall-clock target.

        ``zoneinfo`` permits constructing ambiguous and nonexistent local times.
        A UTC round-trip distinguishes real wall times from DST gaps. For an
        ambiguous time, the first occurrence still in the future is selected.
        For a nonexistent time, advance minute-by-minute to the first valid local
        instant.
        """
        target_wall = datetime.combine(target_date, target_time)
        after_utc = after.astimezone(timezone.utc)
        for minute_offset in range(181):
            wall = target_wall + timedelta(minutes=minute_offset)
            future: list[datetime] = []
            for fold in (0, 1):
                candidate = wall.replace(tzinfo=self.timezone, fold=fold)
                round_trip = candidate.astimezone(timezone.utc).astimezone(
                    self.timezone
                )
                if round_trip.replace(tzinfo=None) != wall:
                    continue
                if candidate.astimezone(timezone.utc) > after_utc:
                    future.append(candidate)
            if future:
                return min(
                    future,
                    key=lambda value: value.astimezone(timezone.utc),
                )

        # Modern IANA transitions are much shorter than three hours. This
        # fallback remains conservative if an unexpected historical rule is
        # encountered.
        fallback = target_wall.replace(tzinfo=self.timezone)
        normalized = fallback.astimezone(timezone.utc).astimezone(self.timezone)
        if normalized.astimezone(timezone.utc) <= after_utc:
            normalized += timedelta(days=1)
        return normalized

    def _quiet_end_for(self, local_now: datetime) -> datetime | None:
        if not self.config.enabled:
            return None
        start = self.config.quiet_start
        end = self.config.quiet_end
        current_time = local_now.timetz().replace(tzinfo=None)

        # Equal boundaries mean a full-day quiet window. This is a deliberate
        # fail-closed interpretation: enabling a schedule with identical times
        # must never unexpectedly permit automation.
        if start == end:
            return self._resolve_local_deadline(
                local_now.date() + timedelta(days=1),
                end,
                after=local_now,
            )

        if start < end:
            if start <= current_time < end:
                return self._resolve_local_deadline(
                    local_now.date(),
                    end,
                    after=local_now,
                )
            return None

        # Overnight quiet window, e.g. 22:00 -> 07:00.
        if current_time >= start:
            return self._resolve_local_deadline(
                local_now.date() + timedelta(days=1),
                end,
                after=local_now,
            )
        if current_time < end:
            return self._resolve_local_deadline(
                local_now.date(),
                end,
                after=local_now,
            )
        return None

    def decision(self, now: datetime | None = None) -> ScheduleDecision:
        local_now = self.local_now(now)
        resume_at = self._quiet_end_for(local_now)
        if resume_at is None:
            return ScheduleDecision(allowed=True, local_now=local_now)
        seconds = max(
            1,
            int(
                (
                    resume_at.astimezone(timezone.utc)
                    - local_now.astimezone(timezone.utc)
                ).total_seconds()
                + 0.999
            ),
        )
        return ScheduleDecision(
            allowed=False,
            local_now=local_now,
            resume_at_local=resume_at,
            retry_after_seconds=seconds,
        )

    def require_active(self, now: datetime | None = None) -> ScheduleDecision:
        """Raise a durable deferral before any Telegram mutation when quiet."""
        decision = self.decision(now)
        if decision.allowed:
            self.logger.debug(
                "Activity schedule allows dispatch: account_id=%s local_time=%s timezone=%s",
                self.account_id,
                decision.local_now.isoformat(timespec="seconds"),
                self.config.timezone_name,
            )
            return decision

        resume = decision.resume_at_local
        self.logger.info(
            "Activity schedule deferred dispatch: account_id=%s local_time=%s "
            "resume_at=%s timezone=%s retry_after=%ss",
            self.account_id,
            decision.local_now.isoformat(timespec="seconds"),
            resume.isoformat(timespec="seconds") if resume else "unknown",
            self.config.timezone_name,
            decision.retry_after_seconds,
        )
        raise DeferredTelegramError(
            "Автоматическая отправка отложена локальным расписанием до "
            f"{resume.strftime('%d.%m %H:%M') if resume else 'разрешённого окна'} "
            f"({self.config.timezone_name})",
            retry_after=decision.retry_after_seconds,
            code="local_quiet_hours",
        )

    async def enforce_schedule(
        self,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        sleep_chunk_seconds: float = 30.0,
    ) -> ScheduleDecision:
        """Wait without blocking the event loop until the local window opens.

        Marlen's durable queue should normally call :meth:`require_active` and
        reschedule long waits instead of occupying its single worker task. This
        method is provided for isolated async callers and tests.
        """
        chunk = max(0.1, min(float(sleep_chunk_seconds), 300.0))
        first_log = True
        while True:
            if cancel_requested is not None and cancel_requested():
                raise asyncio.CancelledError
            decision = self.decision()
            if decision.allowed:
                if not first_log:
                    self.logger.info(
                        "Activity schedule resumed: account_id=%s local_time=%s timezone=%s",
                        self.account_id,
                        decision.local_now.isoformat(timespec="seconds"),
                        self.config.timezone_name,
                    )
                return decision
            if first_log:
                first_log = False
                self.logger.info(
                    "Activity schedule waiting asynchronously: account_id=%s "
                    "local_time=%s resume_at=%s timezone=%s",
                    self.account_id,
                    decision.local_now.isoformat(timespec="seconds"),
                    decision.resume_at_local.isoformat(timespec="seconds")
                    if decision.resume_at_local
                    else "unknown",
                    self.config.timezone_name,
                )
            await asyncio.sleep(min(chunk, decision.retry_after_seconds))


__all__ = [
    "ActivityScheduleConfig",
    "ActivityScheduleManager",
    "ScheduleDecision",
    "SCHEDULE_ENABLED_KEY",
    "TIMEZONE_KEY",
    "QUIET_START_KEY",
    "QUIET_END_KEY",
    "validate_timezone_name",
    "parse_clock",
]
