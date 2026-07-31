"""Pure decisions for resumable channel-link preparation."""

from __future__ import annotations

from enum import StrEnum


class DeferredLinkDisposition(StrEnum):
    PAUSE = "pause"
    LOCAL_BAN = "local_ban"
    SKIP_TARGET = "skip_target"


class LinkErrorDisposition(StrEnum):
    UNKNOWN_BAN = "unknown_ban"
    JOIN_REQUESTED = "join_requested"
    RAISE_RESTRICTION = "raise_restriction"
    STORE_UNAVAILABLE = "store_unavailable"


_RESTRICTION_CODES = frozenset(
    {
        "peer_flood",
        "user_restricted",
        "user_banned",
        "auth_key_duplicated",
        "flood_wait_long",
        "flood_wait_repeated",
        "security_time_sync",
    }
)


def deferred_link_disposition(code: str) -> DeferredLinkDisposition:
    normalized = str(code or "")
    if normalized == "shutdown_before_dispatch":
        return DeferredLinkDisposition.PAUSE
    if normalized == "local_ban_before_dispatch":
        return DeferredLinkDisposition.LOCAL_BAN
    return DeferredLinkDisposition.SKIP_TARGET


def link_error_disposition(code: str) -> LinkErrorDisposition:
    normalized = str(code or "")
    if normalized == "join_result_unknown":
        return LinkErrorDisposition.UNKNOWN_BAN
    if normalized == "join_requested":
        return LinkErrorDisposition.JOIN_REQUESTED
    if normalized in _RESTRICTION_CODES:
        return LinkErrorDisposition.RAISE_RESTRICTION
    return LinkErrorDisposition.STORE_UNAVAILABLE


def group_link_status(is_linked: bool) -> str:
    if is_linked:
        return "Связанное обсуждение · только комментарии к постам"
    return "Группа · локально определена как обычная"
