from __future__ import annotations

import json
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Mapping

from core.campaign_schedule import to_db_time, utc_now
from storage.db_common import DatabaseError

WARMUP_ACTIVITY = "warmup"
WARMUP_CAMPAIGN_CONFLICT_MESSAGE = (
    "Аккаунт сейчас находится на прогреве. "
    "Попробуйте запустить кампанию после окончания прогрева."
)
CAMPAIGN_WARMUP_CONFLICT_MESSAGE = (
    "Для аккаунта уже запущена кампания. "
    "Остановите её перед запуском прогрева."
)
WARMUP_ALREADY_RUNNING_MESSAGE = "Аккаунт уже находится на прогреве."
DEFAULT_WARMUP_LEASE_SECONDS = 30 * 60
MIN_WARMUP_LEASE_SECONDS = 60
MAX_WARMUP_LEASE_SECONDS = 6 * 60 * 60
MAX_ACTIVITY_METADATA_BYTES = 4096
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


def new_activity_owner_token() -> str:
    """Return an unguessable process owner token without exposing account data."""

    return secrets.token_urlsafe(32)


def _lease_seconds(value: int | float | None) -> int:
    try:
        normalized = int(
            DEFAULT_WARMUP_LEASE_SECONDS if value is None else value
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Warmup lease duration must be an integer") from exc
    return max(
        MIN_WARMUP_LEASE_SECONDS,
        min(MAX_WARMUP_LEASE_SECONDS, normalized),
    )


def _owner_token(value: Any) -> str:
    token = str(value or "").strip()
    if len(token) < 16 or len(token) > 256:
        raise ValueError("Warmup owner token is invalid")
    return token


def _account_id(value: Any) -> int:
    try:
        account_id = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Warmup requires a positive account_id") from exc
    if account_id <= 0 or account_id > MAX_SQLITE_INTEGER:
        raise ValueError("Warmup requires a positive 64-bit account_id")
    return account_id


def _metadata_json(value: Mapping[str, Any] | None) -> str:
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Warmup lease metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_ACTIVITY_METADATA_BYTES:
        raise ValueError("Warmup lease metadata is too large")
    return encoded


def prune_expired_account_activity_leases(conn, *, now=None) -> int:
    moment = now or utc_now()
    cursor = conn.execute(
        "DELETE FROM account_activity_leases WHERE lease_until<=?",
        (to_db_time(moment),),
    )
    return max(0, int(cursor.rowcount or 0))


def get_active_account_activity_lease_in_transaction(
    conn,
    account_id,
    *,
    activity: str = WARMUP_ACTIVITY,
    now=None,
) -> dict[str, Any] | None:
    try:
        owner = int(account_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Warmup requires an integer account_id") from exc
    if owner <= 0:
        return None
    if owner > MAX_SQLITE_INTEGER:
        raise ValueError("Warmup requires a positive 64-bit account_id")
    moment = now or utc_now()
    prune_expired_account_activity_leases(conn, now=moment)
    row = conn.execute(
        """SELECT account_id, activity, owner_token, started_at,
                  heartbeat_at, lease_until, metadata_json
           FROM account_activity_leases
           WHERE account_id=? AND activity=? AND lease_until>?
           LIMIT 1""",
        (owner, str(activity), to_db_time(moment)),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["metadata"] = json.loads(result.get("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        result["metadata"] = {}
    return result


class AccountActivityRepositoryMixin(_MixinHost):
    """Durable per-account lease preventing warmup/campaign overlap.

    The lease is intentionally time-bounded. Normal completion releases it
    immediately; after a crash it expires automatically and cannot leave an
    account permanently blocked.
    """

    @staticmethod
    def _prune_expired_account_activity_leases(conn, *, now=None) -> int:
        return prune_expired_account_activity_leases(conn, now=now)

    def _get_active_account_activity_lease_in_transaction(
        self,
        conn,
        account_id,
        *,
        activity: str = WARMUP_ACTIVITY,
        now=None,
    ) -> dict[str, Any] | None:
        return get_active_account_activity_lease_in_transaction(
            conn,
            account_id,
            activity=activity,
            now=now,
        )

    @staticmethod
    def _active_campaign_in_transaction(conn, account_id: int) -> str | None:
        comment = conn.execute(
            """SELECT id FROM comment_campaigns
               WHERE account_id=?
                 AND status IN ('running','paused','network_wait','cycle_wait')
               LIMIT 1""",
            (int(account_id),),
        ).fetchone()
        if comment is not None:
            return "comment"
        joining = conn.execute(
            """SELECT id FROM join_campaigns
               WHERE account_id=?
                 AND status IN ('running','paused','network_wait')
               LIMIT 1""",
            (int(account_id),),
        ).fetchone()
        return "join" if joining is not None else None

    def get_account_activity_lease(
        self, account_id, *, activity: str = WARMUP_ACTIVITY
    ) -> dict[str, Any] | None:
        try:
            with self.get_connection() as conn:
                return self._get_active_account_activity_lease_in_transaction(
                    conn,
                    account_id,
                    activity=activity,
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to read account activity lease: {exc}"
            ) from exc

    def require_account_not_warming(self, account_id) -> None:
        if self.get_account_activity_lease(account_id) is not None:
            raise DatabaseError(WARMUP_CAMPAIGN_CONFLICT_MESSAGE)
        normalized_account_id = int(account_id)
        if normalized_account_id <= 0:
            return
        durable_check = getattr(self, "is_account_in_active_warmup", None)
        if callable(durable_check) and bool(durable_check(normalized_account_id)):
            raise DatabaseError(WARMUP_CAMPAIGN_CONFLICT_MESSAGE)

    def acquire_account_activity_lease(
        self,
        account_id,
        *,
        owner_token,
        lease_seconds: int = DEFAULT_WARMUP_LEASE_SECONDS,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner = _account_id(account_id)
        token = _owner_token(owner_token)
        duration = _lease_seconds(lease_seconds)
        now = utc_now()
        lease_until = now + timedelta(seconds=duration)
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._prune_expired_account_activity_leases(conn, now=now)
                campaign_kind = self._active_campaign_in_transaction(conn, owner)
                if campaign_kind is not None:
                    raise DatabaseError(CAMPAIGN_WARMUP_CONFLICT_MESSAGE)
                existing = conn.execute(
                    """SELECT owner_token FROM account_activity_leases
                       WHERE account_id=? LIMIT 1""",
                    (owner,),
                ).fetchone()
                if existing is not None and str(existing["owner_token"]) != token:
                    raise DatabaseError(WARMUP_ALREADY_RUNNING_MESSAGE)
                if existing is None:
                    conn.execute(
                        """INSERT INTO account_activity_leases(
                               account_id, activity, owner_token, started_at,
                               heartbeat_at, lease_until, metadata_json)
                           VALUES(?, ?, ?, ?, ?, ?, ?)""",
                        (
                            owner,
                            WARMUP_ACTIVITY,
                            token,
                            to_db_time(now),
                            to_db_time(now),
                            to_db_time(lease_until),
                            _metadata_json(metadata),
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE account_activity_leases
                           SET activity=?, heartbeat_at=?, lease_until=?, metadata_json=?
                           WHERE account_id=? AND owner_token=?""",
                        (
                            WARMUP_ACTIVITY,
                            to_db_time(now),
                            to_db_time(lease_until),
                            _metadata_json(metadata),
                            owner,
                            token,
                        ),
                    )
                lease = self._get_active_account_activity_lease_in_transaction(
                    conn, owner, now=now
                )
                if lease is None:
                    raise DatabaseError("Warmup lease was not persisted")
                return lease
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to acquire account warmup lease: {exc}"
            ) from exc

    def renew_account_activity_lease(
        self,
        account_id,
        *,
        owner_token,
        lease_seconds: int = DEFAULT_WARMUP_LEASE_SECONDS,
    ) -> bool:
        owner = _account_id(account_id)
        token = _owner_token(owner_token)
        duration = _lease_seconds(lease_seconds)
        now = utc_now()
        lease_until = now + timedelta(seconds=duration)
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._prune_expired_account_activity_leases(conn, now=now)
                cursor = conn.execute(
                    """UPDATE account_activity_leases
                       SET heartbeat_at=?, lease_until=?
                       WHERE account_id=? AND activity=? AND owner_token=?""",
                    (
                        to_db_time(now),
                        to_db_time(lease_until),
                        owner,
                        WARMUP_ACTIVITY,
                        token,
                    ),
                )
                return int(getattr(cursor, "rowcount", 0) or 0) == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to renew account warmup lease: {exc}"
            ) from exc

    def release_account_activity_lease(
        self, account_id, *, owner_token
    ) -> bool:
        owner = _account_id(account_id)
        token = _owner_token(owner_token)
        try:
            with self.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """DELETE FROM account_activity_leases
                       WHERE account_id=? AND activity=? AND owner_token=?""",
                    (owner, WARMUP_ACTIVITY, token),
                )
                return int(getattr(cursor, "rowcount", 0) or 0) == 1
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to release account warmup lease: {exc}"
            ) from exc
