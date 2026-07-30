from __future__ import annotations

import json
import logging
import os
import secrets
from storage.sqlcipher_driver import dbapi as sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from core.local_security import (
    LocalFileSecurityError,
    ensure_private_directory,
    harden_private_file,
    validate_private_regular_file,
)
from core.paths import APP_PATHS
from core.performance import log_if_slow, wal_size_bytes
from storage.db_account_restrictions import AccountRestrictionRepositoryMixin
from storage.db_accounts import AccountRepositoryMixin
from storage.db_channels import ChannelRepositoryMixin
from storage.db_comment_variants import CommentVariantRepositoryMixin
from storage.db_comment_campaigns import CommentCampaignRepositoryMixin
from storage.db_direct_messages import DirectMessageRepositoryMixin
from storage.db_openai import OpenAIDraftRepositoryMixin
from storage.db_common import (
    DatabaseError as DatabaseError,
    _telegram_id as _telegram_id,
    json_dumps_safe as json_dumps_safe,
)
from storage.db_join_campaigns import JoinCampaignRepositoryMixin
from storage.db_settings import SettingsRepositoryMixin
from storage.db_schema import DatabaseSchemaMixin
from storage.db_tasks import TaskRepositoryMixin
from storage.sqlcipher_driver import (
    default_database_key_storage_dir,
    forget_database_key,
    prepare_encrypted_database,
)

log = logging.getLogger(__name__)
_NATIVE_OS = os


class Database(
    DatabaseSchemaMixin,
    AccountRestrictionRepositoryMixin,
    AccountRepositoryMixin,
    TaskRepositoryMixin,
    ChannelRepositoryMixin,
    CommentVariantRepositoryMixin,
    SettingsRepositoryMixin,
    CommentCampaignRepositoryMixin,
    JoinCampaignRepositoryMixin,
    DirectMessageRepositoryMixin,
    OpenAIDraftRepositoryMixin,
):
    """SQLite compatibility facade composed from domain repositories."""

    ARTIFACT_SECURITY_RECHECK_SECONDS = 300.0

    def __init__(
        self, path=None, *, busy_timeout_ms: int = 30_000, bootstrap: bool = True,
        key_storage_dir: Path | None = None,
    ):
        self.path = Path(path) if path is not None else APP_PATHS.database
        self.key_storage_dir = Path(
            key_storage_dir or default_database_key_storage_dir(self.path)
        )
        try:
            ensure_private_directory(self.path.parent)
            if self.path.exists() or self.path.is_symlink():
                validate_private_regular_file(self.path)
        except LocalFileSecurityError as exc:
            raise DatabaseError(f"Unsafe SQLite path: {exc}") from exc
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        # Opening a connection creates the file before any header is written.
        # If this constructor fails afterwards it must not leave a zero-byte
        # marlen.db behind: prepare_encrypted_database() refuses to initialize
        # an existing empty file, so such a leftover permanently blocks every
        # later startup even though it holds no data.
        try:
            preexisting = self.path.exists() or self.path.is_symlink()
        except OSError:
            preexisting = True
        try:
            self._database_key = prepare_encrypted_database(
                self.path, key_storage_dir=self.key_storage_dir
            )
            self.sqlite_timeout_seconds = self.busy_timeout_ms / 1000.0
            self._init_schema(bootstrap=bootstrap)
        except BaseException:
            if not preexisting:
                self._discard_empty_database_file()
            raise

    def _discard_empty_database_file(self) -> None:
        """Remove a zero-byte database file this constructor just created."""

        try:
            if not self.path.is_file() or self.path.is_symlink():
                return
            if self.path.stat().st_size != 0:
                return
        except OSError:
            return
        try:
            self.close_thread_connection()
        except Exception:
            log.debug("Could not close the connection to an empty database file")
        try:
            self.path.unlink()
        except OSError:
            log.warning("Could not remove the empty database file %s", self.path)
        forget_database_key(self.path)

    def _init_schema(self, *, bootstrap: bool) -> None:
        self._thread_connections = threading.local()
        self._artifact_security_lock = threading.RLock()
        self._artifact_security_identities: dict[Path, tuple[int, int, int]] = {}
        self._artifact_security_markers: dict[Path, bytes] = {}
        self._artifact_security_last_check = 0.0
        self._artifact_security_failure: BaseException | None = None
        raw_version = self._raw_user_version()
        self._harden_database_artifacts(force=True)
        if raw_version > self.SCHEMA_VERSION:
            raise DatabaseError(
                f"Database schema v{raw_version} is newer than supported v{self.SCHEMA_VERSION}"
            )
        if bootstrap:
            self._ensure_wal_mode()
            # WAL/SHM may be created by enabling WAL after the initial check.
            self._harden_database_artifacts(force=True)
        if raw_version == self.SCHEMA_VERSION:
            if bootstrap:
                self.recover_stale_deliveries()
            return
        if not bootstrap:
            raise DatabaseError(
                f"Database schema v{raw_version} requires bootstrap to v{self.SCHEMA_VERSION}"
            )
        if raw_version < self.LEGACY_SCHEMA_VERSION:
            self.init()
        self.run_migrations()
        self.recover_stale_deliveries()

    @staticmethod
    def _artifact_security_identity(info) -> tuple[int, int, int]:
        """Return an identity that changes when an artifact is replaced.

        Size and modification time are intentionally excluded because SQLite
        updates WAL/SHM contents during normal transactions.  Creation time is
        stable on Windows and birth time is stable on platforms that expose it,
        so either can distinguish a recreated sidecar even when the filesystem
        immediately reuses the same inode.
        """

        inode = int(getattr(info, "st_ino", 0) or 0)
        creation_ns = int(
            getattr(info, "st_birthtime_ns", 0)
            or (getattr(info, "st_ctime_ns", 0) if os.name == "nt" else 0)
            or 0
        )
        return int(getattr(info, "st_dev", 0) or 0), inode, creation_ns

    @staticmethod
    def _read_artifact_security_marker(path: Path) -> bytes | None:
        """Read a marker bound to the current filesystem object.

        POSIX extended attributes and Windows alternate data streams survive
        normal SQLite writes but disappear when a WAL/SHM file is unlinked and
        recreated.  That makes replacement detection reliable even when a
        filesystem immediately reuses the same inode and timestamps.
        """

        candidate = Path(path)
        if _NATIVE_OS.name == "nt":
            try:
                with open(f"{candidate}:marlen_security_id", "rb") as stream:
                    value = stream.read(64)
                return value or None
            except (FileNotFoundError, OSError):
                return None
        getxattr = getattr(_NATIVE_OS, "getxattr", None)
        if getxattr is None:
            return None
        try:
            value = getxattr(
                candidate, b"user.marlen.security_id", follow_symlinks=False
            )
            return bytes(value) or None
        except (AttributeError, OSError):
            return None

    @staticmethod
    def _write_artifact_security_marker(path: Path, marker: bytes) -> bool:
        candidate = Path(path)
        value = bytes(marker)
        if _NATIVE_OS.name == "nt":
            try:
                with open(f"{candidate}:marlen_security_id", "wb") as stream:
                    stream.write(value)
                return True
            except OSError:
                return False
        setxattr = getattr(_NATIVE_OS, "setxattr", None)
        if setxattr is None:
            return False
        try:
            setxattr(
                candidate,
                b"user.marlen.security_id",
                value,
                follow_symlinks=False,
            )
            return True
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _caused_by_missing_file(exc: BaseException) -> bool:
        """Return whether an exception chain represents an artifact that vanished."""

        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, FileNotFoundError):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _validate_database_artifact(self, candidate: Path) -> os.stat_result | None:
        """Validate one artifact while tolerating normal WAL sidecar churn.

        SQLite may remove ``-wal``, ``-shm`` or ``-journal`` after the existence
        check and before ``lstat``.  The main database file remains fail-closed.
        A sidecar that reappears is validated again instead of being trusted.
        """

        attempts = 1 if candidate == self.path else 3
        last_error: LocalFileSecurityError | None = None
        for _attempt in range(attempts):
            try:
                return validate_private_regular_file(candidate, harden=False)
            except LocalFileSecurityError as exc:
                last_error = exc
                if candidate == self.path or not self._caused_by_missing_file(exc):
                    raise
                try:
                    exists = candidate.exists() or candidate.is_symlink()
                except OSError as inspect_exc:
                    raise LocalFileSecurityError(
                        f"Could not inspect local path {candidate}: {inspect_exc}"
                    ) from inspect_exc
                if not exists:
                    return None
                # The sidecar was recreated between calls. Validate the new file.
                continue
        if last_error is None:
            raise LocalFileSecurityError(
                f"Could not validate local path {candidate} after repeated attempts"
            )
        raise last_error

    def _harden_database_artifacts(self, *, force: bool = False) -> None:
        """Keep SQLite artifacts private without spawning ACL tools per query.

        On Windows, applying ACLs uses ``icacls.exe``.  The old implementation
        launched it for the database, WAL and SHM after *every* repository read
        and write.  A cheap inode/mode fingerprint runs after transactions; full validation
        and permission hardening run only for changed artifacts or on the periodic
        security interval.
        """

        candidates = (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            Path(f"{self.path}-journal"),
        )
        now = time.monotonic()
        recheck_seconds = max(
            1.0,
            float(getattr(self, "ARTIFACT_SECURITY_RECHECK_SECONDS", 60.0)),
        )
        last_check = float(getattr(self, "_artifact_security_last_check", 0.0) or 0.0)
        periodic_due = now - last_check >= recheck_seconds
        refresh_needed = bool(force)
        identities = getattr(self, "_artifact_security_identities", None)
        if identities is None:
            identities = {}
            self._artifact_security_identities = identities
        markers = getattr(self, "_artifact_security_markers", None)
        if markers is None:
            markers = {}
            self._artifact_security_markers = markers
        if not force and now - last_check < recheck_seconds:
            # Cheap fingerprint pass: avoid chmod/icacls and full validation for
            # unchanged artifacts, but do not miss a recreated WAL/SHM, replaced
            # database file or newly broadened POSIX mode.
            for candidate in candidates:
                try:
                    exists = candidate.exists() or candidate.is_symlink()
                    if not exists:
                        if candidate in self._artifact_security_identities:
                            refresh_needed = True
                            break
                        continue
                    info = candidate.lstat()
                except OSError:
                    refresh_needed = True
                    break
                identity = self._artifact_security_identity(info)
                if identities.get(candidate) != identity:
                    refresh_needed = True
                    break
                known_marker = markers.get(candidate)
                if known_marker and (
                    self._read_artifact_security_marker(candidate) != known_marker
                ):
                    refresh_needed = True
                    break
                # Windows ``st_mode`` reflects the DOS read-only flag, not the
                # effective ACL applied by ``icacls``. Treating it as a POSIX
                # mode makes every transaction look insecure and defeats the
                # ACL identity cache.
                if os.name != "nt" and (int(info.st_mode) & 0o777) != 0o600:
                    refresh_needed = True
                    break
            if not refresh_needed:
                return
        with self._artifact_security_lock:
            # Another thread may have completed the periodic check while this
            # caller waited for the lock.
            last_check = float(
                getattr(self, "_artifact_security_last_check", 0.0) or 0.0
            )
            if not refresh_needed and now - last_check < recheck_seconds:
                return
            present: set[Path] = set()
            for candidate in candidates:
                try:
                    exists = candidate.exists() or candidate.is_symlink()
                except OSError as exc:
                    raise DatabaseError(
                        f"Could not inspect SQLite artifact {candidate}: {exc}"
                    ) from exc
                if not exists:
                    identities.pop(candidate, None)
                    markers.pop(candidate, None)
                    continue
                try:
                    validated_info = self._validate_database_artifact(candidate)
                except LocalFileSecurityError as exc:
                    raise DatabaseError(f"Unsafe SQLite artifact: {exc}") from exc
                if validated_info is None:
                    identities.pop(candidate, None)
                    markers.pop(candidate, None)
                    continue
                present.add(candidate)

                identity = self._artifact_security_identity(validated_info)
                known_identity = identities.get(candidate)
                mode_is_private = os.name == "nt" or (
                    (int(validated_info.st_mode) & 0o777) == 0o600
                )
                known_marker = markers.get(candidate)
                marker_matches = known_marker == b"" or (
                    bool(known_marker)
                    and self._read_artifact_security_marker(candidate) == known_marker
                )
                should_harden = (
                    known_identity != identity
                    or not marker_matches
                    or not mode_is_private
                    # Windows ACLs cannot be validated cheaply from ``stat``.
                    # Re-apply them once per periodic security check, not after
                    # every repository transaction.
                    or (os.name == "nt" and (force or periodic_due))
                )
                if should_harden:
                    verified = None
                    current_info = validated_info
                    for _attempt in range(3):
                        current_identity = self._artifact_security_identity(
                            current_info
                        )
                        if harden_private_file(candidate):
                            try:
                                verified = self._validate_database_artifact(candidate)
                            except LocalFileSecurityError as exc:
                                raise DatabaseError(
                                    f"Unsafe SQLite artifact: {exc}"
                                ) from exc
                            break

                        # The main database must never disappear or be replaced
                        # while its security properties are being applied.
                        if candidate == self.path:
                            break

                        # SQLite can checkpoint and replace/remove WAL, SHM or
                        # rollback-journal files between lstat(), chmod()/ACL and
                        # verification. Revalidate the object that is present now.
                        try:
                            replacement = self._validate_database_artifact(candidate)
                        except LocalFileSecurityError as exc:
                            raise DatabaseError(
                                f"Unsafe SQLite artifact: {exc}"
                            ) from exc
                        if replacement is None:
                            verified = None
                            break
                        replacement_identity = self._artifact_security_identity(
                            replacement
                        )
                        if (
                            os.name != "nt"
                            and (int(replacement.st_mode) & 0o777) == 0o600
                        ):
                            # A sidecar can be replaced between validation and
                            # chmod, and some filesystems immediately reuse the
                            # inode.  A currently regular, non-symlink, private
                            # POSIX file is already safe regardless of whether
                            # its coarse identity happens to match the old one.
                            verified = replacement
                            break
                        if replacement_identity == current_identity:
                            # The same non-private object remains and could not
                            # be restricted: this is a real security failure.
                            break
                        current_info = replacement

                    if verified is None:
                        if candidate != self.path:
                            try:
                                still_present = (
                                    candidate.exists() or candidate.is_symlink()
                                )
                            except OSError as exc:
                                raise DatabaseError(
                                    f"Could not inspect SQLite artifact {candidate}: {exc}"
                                ) from exc
                            if not still_present:
                                present.discard(candidate)
                                identities.pop(candidate, None)
                                markers.pop(candidate, None)
                                continue
                        raise DatabaseError(
                            f"Unsafe SQLite artifact: could not restrict private file {candidate}"
                        )
                    identity = self._artifact_security_identity(verified)

                if known_marker == b"":
                    # This filesystem does not support a durable object marker.
                    # Keep the identity fallback without re-running chmod/icacls
                    # after every normal SQLite transaction.
                    markers[candidate] = b""
                else:
                    marker = (
                        known_marker
                        if marker_matches and known_marker is not None
                        else secrets.token_bytes(16)
                    )
                    if self._write_artifact_security_marker(candidate, marker):
                        if self._read_artifact_security_marker(candidate) == marker:
                            markers[candidate] = marker
                        else:
                            markers[candidate] = b""
                    else:
                        markers[candidate] = b""

                # Writing an xattr can update ctime on some filesystems. Store
                # the final identity so the next cheap pass remains a true no-op.
                try:
                    final_info = candidate.lstat()
                except OSError as exc:
                    if candidate == self.path:
                        raise DatabaseError(
                            f"Could not inspect SQLite artifact {candidate}: {exc}"
                        ) from exc
                    present.discard(candidate)
                    identities.pop(candidate, None)
                    markers.pop(candidate, None)
                    continue
                identities[candidate] = self._artifact_security_identity(final_info)

            for stale in set(identities) - present:
                identities.pop(stale, None)
                markers.pop(stale, None)
            self._artifact_security_last_check = time.monotonic()

    def recover_stale_deliveries(
        self, *, stale_after_seconds: int = 300
    ) -> dict[str, object]:
        """Move stale crash-interrupted reservations to manual-review state.

        The update is idempotent.  In addition to aggregate totals, the result
        carries per-account counts so the GUI journal can attribute maintenance
        events to the actual owner instead of whichever account is currently
        selected.
        """

        threshold = max(60, int(stale_after_seconds))
        modifier = f"-{threshold} seconds"
        started = time.monotonic()
        per_account: dict[int, dict[str, int]] = {}

        def note(account_id: int, key: str) -> None:
            owner = max(0, int(account_id or 0))
            bucket = per_account.setdefault(
                owner,
                {
                    "comment_deliveries": 0,
                    "direct_message_deliveries": 0,
                    "total": 0,
                },
            )
            bucket[key] += 1
            bucket["total"] += 1

        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            comment_rows = conn.execute(
                """SELECT id, account_id
                   FROM comment_deliveries
                   WHERE status='sending'
                     AND reserved_at <= datetime('now', ?)""",
                (modifier,),
            ).fetchall()
            direct_rows = conn.execute(
                """SELECT d.id, t.payload
                   FROM direct_message_deliveries d
                   LEFT JOIN tasks t ON t.id=d.task_id
                   WHERE d.status='sending'
                     AND d.reserved_at <= datetime('now', ?)""",
                (modifier,),
            ).fetchall()

            comment_count = conn.execute(
                """UPDATE comment_deliveries
                   SET status='uncertain',
                       error=COALESCE(error,
                           'Recovered stale delivery after unclean shutdown; manual review required'),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status='sending'
                     AND reserved_at <= datetime('now', ?)""",
                (modifier,),
            ).rowcount
            direct_count = conn.execute(
                """UPDATE direct_message_deliveries
                   SET status='uncertain',
                       error=COALESCE(error,
                           'Recovered stale delivery after unclean shutdown; manual review required'),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status='sending'
                     AND reserved_at <= datetime('now', ?)""",
                (modifier,),
            ).rowcount

            for row in comment_rows:
                note(int(row["account_id"] or 0), "comment_deliveries")
            for row in direct_rows:
                owner = 0
                try:
                    payload = json.loads(row["payload"] or "{}")
                    if isinstance(payload, dict):
                        owner = int(payload.get("account_id") or 0)
                except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
                    owner = 0
                note(owner, "direct_message_deliveries")

        comment_deliveries = max(0, int(comment_count or 0))
        direct_deliveries = max(0, int(direct_count or 0))
        result: dict[str, object] = {
            "comment_deliveries": comment_deliveries,
            "direct_message_deliveries": direct_deliveries,
            "accounts": per_account,
        }
        result["total"] = comment_deliveries + direct_deliveries
        if result["total"]:
            log.warning(
                "Recovered %s stale delivery reservation(s) as uncertain; manual review required: %s",
                result["total"],
                result,
            )
        log_if_slow(
            log,
            "recover_stale_deliveries",
            started,
            threshold_seconds=0.5,
            recovered=result["total"],
        )
        return result

    def _recover_stale_deliveries(self):
        """Compatibility alias for older callers and tests."""
        return self.recover_stale_deliveries()

    def log_wal_size_if_large(self, *, warning_bytes: int = 64 * 1024 * 1024) -> int:
        """Log an oversized local WAL without changing SQLite tuning."""

        size = wal_size_bytes(self.path)
        if size >= max(1, int(warning_bytes)):
            log.warning(
                "SQLite WAL is large: path=%s size_bytes=%s threshold_bytes=%s",
                self.path,
                size,
                int(warning_bytes),
            )
        return size

    def restore_processing_tasks(self):
        """Compatibility alias for startup recovery."""
        return self.reset_running_tasks()

    def _thread_connection(self):
        conn = getattr(self._thread_connections, "connection", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self.sqlite_timeout_seconds,
                key=self._database_key,
                key_storage_dir=self.key_storage_dir,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._thread_connections.connection = conn
        return conn

    @contextmanager
    def get_connection(self):
        """Reuse one connection per thread and commit only the outermost scope.

        Repository methods frequently call one another. A nested context must not
        commit the caller's transaction; any nested failure marks the whole outer
        unit of work for rollback, even if an intermediate layer catches it.
        """
        conn = self._thread_connection()
        state = self._thread_connections
        depth = int(getattr(state, "transaction_depth", 0))
        outer_started = time.monotonic() if depth == 0 else None
        if depth == 0:
            pending_security_failure = getattr(
                self, "_artifact_security_failure", None
            )
            if pending_security_failure is not None:
                try:
                    self._harden_database_artifacts(force=True)
                except Exception as exc:
                    raise DatabaseError(
                        "SQLite artifact security check failed before transaction: "
                        f"{exc}"
                    ) from exc
                self._artifact_security_failure = None
            state.rollback_only = False
        state.transaction_depth = depth + 1
        active_error = False
        committed = False
        try:
            yield conn
        except sqlite3.Error as exc:
            active_error = True
            state.rollback_only = True
            log.error("Database error: %s", exc)
            raise DatabaseError(f"Database error: {exc}") from exc
        except BaseException:
            active_error = True
            state.rollback_only = True
            raise
        finally:
            state.transaction_depth = max(0, int(state.transaction_depth) - 1)
            if state.transaction_depth == 0:
                rollback_only = bool(getattr(state, "rollback_only", False))
                try:
                    if rollback_only:
                        conn.rollback()
                    else:
                        conn.commit()
                        committed = True
                except sqlite3.Error as exc:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    if not active_error:
                        raise DatabaseError(
                            f"Database transaction failed: {exc}"
                        ) from exc
                finally:
                    state.rollback_only = False
                    if outer_started is not None:
                        log_if_slow(
                            log,
                            "sqlite_transaction",
                            outer_started,
                            threshold_seconds=0.5,
                        )

                hardening_started = time.monotonic()
                try:
                    self._harden_database_artifacts()
                except Exception as exc:
                    self._artifact_security_failure = exc
                    if committed:
                        # The transaction is already durable. Reporting it as a
                        # failed write would make callers repeat non-idempotent
                        # state changes or roll back external files incorrectly.
                        # Block the next transaction at its entry instead.
                        log.critical(
                            "SQLite transaction committed, but artifact security "
                            "verification failed; future transactions are blocked",
                            exc_info=True,
                        )
                    elif not active_error:
                        raise DatabaseError(
                            "SQLite artifact security verification failed: "
                            f"{exc}"
                        ) from exc
                    else:
                        log.error(
                            "SQLite artifact security verification also failed "
                            "while rolling back another error",
                            exc_info=True,
                        )
                else:
                    self._artifact_security_failure = None
                finally:
                    log_if_slow(
                        log,
                        "sqlite_artifact_hardening",
                        hardening_started,
                        threshold_seconds=0.5,
                    )

    def close_thread_connection(self) -> None:
        state = getattr(self, "_thread_connections", None)
        if state is None:
            return
        conn = getattr(state, "connection", None)
        if conn is None:
            return
        try:
            conn.close()
        finally:
            state.connection = None

    def __del__(self) -> None:
        # Production owners close explicitly. This guard prevents a same-thread
        # Database object abandoned by a caller/test from reaching sqlite3's GC
        # finalizer with an open connection. Connections owned by worker threads
        # are still closed by their explicit cleanup path.
        try:
            self.close_thread_connection()
        except Exception:
            pass

    close = close_thread_connection
