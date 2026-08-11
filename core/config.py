from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

from core.paths import APP_PATHS

log = logging.getLogger(__name__)

DEFAULT_MAX_CHANNELS_PER_RUN = 40
DEFAULT_CAMPAIGN_HOURS = 24
DEFAULT_MAX_JOINS_PER_HOUR = 40
DEFAULT_MIN_JOIN_INTERVAL_SECONDS = 45
DEFAULT_POST_JOIN_DELAY_MIN_SECONDS = 15
DEFAULT_POST_JOIN_DELAY_MAX_SECONDS = 30
DEFAULT_LINK_JOIN_DELAY_MIN_SECONDS = 15
DEFAULT_LINK_JOIN_DELAY_MAX_SECONDS = 25
# Live pacing: target roughly one completed link check every two minutes.
# This delay is separate from JOIN/FloodWait safety waits and remains
# environment-overridable through MARLEN_LINK_CHECK_DELAY_*.
DEFAULT_LINK_CHECK_DELAY_MIN_SECONDS = 105
DEFAULT_LINK_CHECK_DELAY_MAX_SECONDS = 135
MIN_COMMENT_VARIANTS = 1
DEFAULT_COMMENT_VARIANTS = 10
MAX_COMMENT_VARIANTS = 10


def _env_int(
    name: str, default: int, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError:
            log.warning("Invalid integer in %s=%r; using %s", name, raw, default)
            value = default
    if minimum is not None and value < minimum:
        log.warning(
            "%s=%s is below minimum %s; using %s", name, value, minimum, minimum
        )
        value = minimum
    if maximum is not None and value > maximum:
        log.warning(
            "%s=%s is above maximum %s; using %s", name, value, maximum, maximum
        )
        value = maximum
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError:
            log.warning("Invalid number in %s=%r; using %s", name, raw, default)
            value = default
    if not math.isfinite(value):
        log.warning("Non-finite number in %s=%r; using %s", name, raw, default)
        value = default
    if minimum is not None and value < minimum:
        log.warning(
            "%s=%s is below minimum %s; using %s", name, value, minimum, minimum
        )
        value = minimum
    if maximum is not None and value > maximum:
        log.warning(
            "%s=%s is above maximum %s; using %s", name, value, maximum, maximum
        )
        value = maximum
    return value


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str
    session_dir: Path
    session_name: str = "main"
    account_id: int | None = None
    phone: str | None = None
    proxy_enabled: bool = False
    proxy_type: str = "SOCKS5"
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    expected_account_id: int | None = None

    @property
    def configured(self) -> bool:
        return self.api_id > 0 and bool(self.api_hash)


@dataclass(frozen=True)
class QueueSettings:
    workers: int = 1
    max_retries: int = 3


class Config:
    """Runtime configuration with one stable interface for all services."""

    def __init__(self) -> None:
        APP_PATHS.ensure()
        self.paths = APP_PATHS
        self.telegram = TelegramSettings(
            api_id=_env_int("API_ID", 0, minimum=0),
            api_hash=os.getenv("API_HASH", "").strip(),
            session_dir=APP_PATHS.sessions,
            phone=os.getenv("PHONE") or None,
            proxy_enabled=False,
        )
        requested_workers = _env_int("WORKERS", 1, minimum=1, maximum=16)
        if requested_workers != 1:
            log.warning(
                "This GUI build uses one queue QThread; WORKERS=%s is ignored",
                requested_workers,
            )
        self.queue = QueueSettings(
            workers=1,
            max_retries=_env_int("MAX_RETRIES", 3, minimum=0, maximum=100),
        )
        raw_db_path = os.getenv("SQLITE_PATH", "").strip()
        self.database_path = (
            Path(raw_db_path).expanduser().resolve()
            if raw_db_path
            else APP_PATHS.database
        )
        self.rate_limit = _env_float("RATE_LIMIT", 1.0, minimum=1.0, maximum=3600.0)
        self.max_channels_per_run = _env_int(
            "MAX_CHANNELS_PER_RUN",
            DEFAULT_MAX_CHANNELS_PER_RUN,
            minimum=1,
            maximum=1000,
        )
        self.campaign_hours = _env_int(
            "COMMENT_CAMPAIGN_HOURS",
            DEFAULT_CAMPAIGN_HOURS,
            minimum=1,
            maximum=168,
        )
        self.max_joins_per_hour = _env_int(
            "MAX_JOINS_PER_HOUR",
            DEFAULT_MAX_JOINS_PER_HOUR,
            minimum=1,
            maximum=1000,
        )
        self.min_join_interval_seconds = _env_int(
            "MIN_JOIN_INTERVAL_SECONDS",
            DEFAULT_MIN_JOIN_INTERVAL_SECONDS,
            minimum=15,
            maximum=86_400,
        )
        self.post_join_delay_min_seconds = _env_int(
            "POST_JOIN_DELAY_MIN_SECONDS",
            DEFAULT_POST_JOIN_DELAY_MIN_SECONDS,
            minimum=1,
            maximum=3600,
        )
        self.post_join_delay_max_seconds = _env_int(
            "POST_JOIN_DELAY_MAX_SECONDS",
            DEFAULT_POST_JOIN_DELAY_MAX_SECONDS,
            minimum=self.post_join_delay_min_seconds,
            maximum=3600,
        )
        self.link_join_delay_min_seconds = _env_int(
            "LINK_JOIN_DELAY_MIN_SECONDS",
            DEFAULT_LINK_JOIN_DELAY_MIN_SECONDS,
            minimum=1,
            maximum=3600,
        )
        self.link_join_delay_max_seconds = _env_int(
            "LINK_JOIN_DELAY_MAX_SECONDS",
            DEFAULT_LINK_JOIN_DELAY_MAX_SECONDS,
            minimum=self.link_join_delay_min_seconds,
            maximum=3600,
        )
        self.link_check_delay_min_seconds = _env_int(
            "LINK_CHECK_DELAY_MIN_SECONDS",
            DEFAULT_LINK_CHECK_DELAY_MIN_SECONDS,
            minimum=1,
            maximum=300,
        )
        self.link_check_delay_max_seconds = _env_int(
            "LINK_CHECK_DELAY_MAX_SECONDS",
            DEFAULT_LINK_CHECK_DELAY_MAX_SECONDS,
            minimum=self.link_check_delay_min_seconds,
            maximum=300,
        )

    @property
    def TELEGRAM(self):
        """Compatibility view for old, non-production modules."""
        return type(
            "TelegramCompat",
            (),
            {
                "API_ID": self.telegram.api_id,
                "API_HASH": self.telegram.api_hash,
                "SESSION": str(self.telegram.session_dir / "main"),
            },
        )

    PATHS = APP_PATHS
