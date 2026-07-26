"""Shutdown-contract and crash-recovery tests for the critical storage layer.

These cover behaviour that the release gate in tools/check_critical_coverage.py
declares critical but that no test exercised: the non-blocking GUI-thread
finalization contract, the cooperative shutdown fallback, and the stale-delivery
recovery that runs after an unclean shutdown.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_schedule import from_db_time, utc_now
from storage.database import Database, DatabaseError


# --------------------------------------------------------------------------
# ApplicationContainer.finalize_shutdown / shutdown
# --------------------------------------------------------------------------


class _FakeWorker:
    def __init__(self, running: bool = False, stops: bool = True) -> None:
        self._running = running
        self._stops = stops
        self.stop_calls: list[int] = []

    def isRunning(self) -> bool:  # noqa: N802 - mirrors the QThread API
        return self._running

    def stop(self, timeout_ms: int) -> bool:
        self.stop_calls.append(timeout_ms)
        if self._stops:
            self._running = False
        return self._stops


def _container(tmp_path: Path, worker: _FakeWorker, *, migration_stopped: bool = True):
    from core.composition import ApplicationContainer

    database = Database(tmp_path / "shutdown.db")
    container = object.__new__(ApplicationContainer)
    container.database = database
    container.queue_worker = worker
    container.api = SimpleNamespace(
        prepare_shutdown=lambda: None,
        wait_for_secret_migration=lambda _timeout: migration_stopped,
    )
    return container, database


def test_finalize_shutdown_refuses_while_the_queue_worker_still_runs(
    tmp_path: Path,
) -> None:
    """The GUI thread must not close SQLite out from under a live worker."""

    worker = _FakeWorker(running=True)
    container, database = _container(tmp_path, worker)
    try:
        assert container.finalize_shutdown() is False
        # The connection must still be usable after a refused finalization.
        database.set_setting("probe", "alive")
        assert database.get_setting("probe") == "alive"
    finally:
        database.close_thread_connection()


def test_finalize_shutdown_refuses_while_secret_migration_is_running(
    tmp_path: Path,
) -> None:
    worker = _FakeWorker(running=False)
    container, database = _container(tmp_path, worker, migration_stopped=False)
    try:
        assert container.finalize_shutdown() is False
    finally:
        database.close_thread_connection()


def test_finalize_shutdown_closes_the_connection_once_everything_stopped(
    tmp_path: Path,
) -> None:
    worker = _FakeWorker(running=False)
    container, database = _container(tmp_path, worker)
    try:
        database.set_setting("probe", "value")
        assert container.finalize_shutdown() is True
        # close_thread_connection() drops the thread-local handle; a later call
        # must transparently reopen instead of raising.
        assert database.get_setting("probe") == "value"
    finally:
        database.close_thread_connection()


def test_shutdown_stops_the_worker_and_reports_success(tmp_path: Path) -> None:
    worker = _FakeWorker(running=True, stops=True)
    container, database = _container(tmp_path, worker)
    try:
        assert container.shutdown(timeout_ms=1_234) is True
        assert worker.stop_calls == [1_234]
    finally:
        database.close_thread_connection()


def test_shutdown_reports_failure_when_the_worker_refuses_to_stop(
    tmp_path: Path,
) -> None:
    """A stuck worker must be reported, never silently treated as stopped."""

    worker = _FakeWorker(running=True, stops=False)
    container, database = _container(tmp_path, worker)
    try:
        assert container.shutdown(timeout_ms=10) is False
    finally:
        database.close_thread_connection()


def test_shutdown_reports_failure_when_secret_migration_does_not_finish(
    tmp_path: Path,
) -> None:
    worker = _FakeWorker(running=False)
    container, database = _container(tmp_path, worker, migration_stopped=False)
    try:
        assert container.shutdown(timeout_ms=10) is False
    finally:
        database.close_thread_connection()


# --------------------------------------------------------------------------
# recover_stale_deliveries: what happens after an unclean shutdown
# --------------------------------------------------------------------------


def _reserve(database: Database, *, account_id: int, channel_id: int, post_id: int):
    assert database.reserve_comment_delivery(
        channel_id,
        post_id,
        linked_chat_id=-100_000 - channel_id,
        text="text",
        account_id=account_id,
        campaign_id=0,
        action_type="campaign_comment",
    )


def _age_reservations(database: Database, seconds: int) -> None:
    stamp = (utc_now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    with database.get_connection() as conn:
        conn.execute("UPDATE comment_deliveries SET reserved_at=?", (stamp,))


def test_stale_reservations_become_uncertain_never_sent(tmp_path: Path) -> None:
    """A crash mid-send must leave an unprovable delivery, not a success."""

    database = Database(tmp_path / "recovery.db")
    try:
        database.set_setting("telegram.account_id", 5)
        _reserve(database, account_id=5, channel_id=-1001, post_id=10)
        _age_reservations(database, 3_600)

        result = database.recover_stale_deliveries()
        assert int(result["comment_deliveries"]) == 1
        assert int(result["total"]) == 1

        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT status, error FROM comment_deliveries"
            ).fetchone()
        assert row["status"] == "uncertain"
        assert "manual review" in str(row["error"])
    finally:
        database.close_thread_connection()


def test_stale_recovery_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "recovery-twice.db")
    try:
        database.set_setting("telegram.account_id", 5)
        _reserve(database, account_id=5, channel_id=-1001, post_id=10)
        _age_reservations(database, 3_600)

        assert int(database.recover_stale_deliveries()["total"]) == 1
        # Running maintenance again must not re-count or re-touch the same row.
        assert int(database.recover_stale_deliveries()["total"]) == 0
    finally:
        database.close_thread_connection()


def test_fresh_reservations_are_left_alone(tmp_path: Path) -> None:
    """An in-flight send must not be declared uncertain by maintenance."""

    database = Database(tmp_path / "recovery-fresh.db")
    try:
        database.set_setting("telegram.account_id", 5)
        _reserve(database, account_id=5, channel_id=-1001, post_id=10)

        assert int(database.recover_stale_deliveries()["total"]) == 0
        with database.get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM comment_deliveries"
            ).fetchone()["status"]
        assert status == "sending"
    finally:
        database.close_thread_connection()


def test_recovery_attributes_rows_to_their_owning_account(tmp_path: Path) -> None:
    """The journal must blame the real owner, not the selected account."""

    database = Database(tmp_path / "recovery-accounts.db")
    try:
        database.set_setting("telegram.account_id", 5)
        _reserve(database, account_id=5, channel_id=-1001, post_id=10)
        _reserve(database, account_id=9, channel_id=-1002, post_id=11)
        _age_reservations(database, 3_600)

        result = database.recover_stale_deliveries()
        per_account = result["accounts"]
        assert isinstance(per_account, dict)
        assert int(per_account[5]["total"]) == 1
        assert int(per_account[9]["total"]) == 1
    finally:
        database.close_thread_connection()


def test_stale_threshold_has_a_safe_lower_bound(tmp_path: Path) -> None:
    """A caller must not be able to declare a one-second-old send uncertain."""

    database = Database(tmp_path / "recovery-threshold.db")
    try:
        database.set_setting("telegram.account_id", 5)
        _reserve(database, account_id=5, channel_id=-1001, post_id=10)
        _age_reservations(database, 30)

        assert int(database.recover_stale_deliveries(stale_after_seconds=1)["total"]) == 0
    finally:
        database.close_thread_connection()


# --------------------------------------------------------------------------
# Transaction and connection lifecycle
# --------------------------------------------------------------------------


def test_a_failing_nested_scope_rolls_back_the_whole_unit_of_work(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "rollback.db")
    try:
        database.set_setting("keep", "before")
        with pytest.raises(RuntimeError):
            with database.get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES('keep', 'after')"
                )
                raise RuntimeError("caller failed halfway")
        assert database.get_setting("keep") == "before"
    finally:
        database.close_thread_connection()


def test_sqlite_errors_are_normalized_to_database_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "errors.db")
    try:
        with pytest.raises(DatabaseError):
            with database.get_connection() as conn:
                conn.execute("SELECT * FROM table_that_does_not_exist")
    finally:
        database.close_thread_connection()


def test_close_thread_connection_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "close-twice.db")
    database.set_setting("k", "v")
    database.close_thread_connection()
    database.close_thread_connection()
    assert database.get_setting("k") == "v"
    database.close_thread_connection()


def test_reset_running_tasks_returns_interrupted_work_to_the_queue(
    tmp_path: Path,
) -> None:
    """After a crash a 'running' task must become claimable again, not be lost."""

    database = Database(tmp_path / "reset-running.db")
    try:
        task_id = database.insert_task("noop", {})
        claimed = database.claim_next_pending_task()
        assert claimed is not None and int(claimed["id"]) == task_id
        assert database.claim_next_pending_task() is None

        database.reset_running_tasks()

        again = database.claim_next_pending_task()
        assert again is not None and int(again["id"]) == task_id
    finally:
        database.close_thread_connection()


def test_wal_size_probe_reports_without_changing_tuning(tmp_path: Path) -> None:
    database = Database(tmp_path / "wal.db")
    try:
        database.set_setting("k", "v")
        assert database.log_wal_size_if_large(warning_bytes=1) >= 0
        with database.get_connection() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        database.close_thread_connection()


def test_queued_slot_survives_a_reopen_and_keeps_its_schedule(tmp_path: Path) -> None:
    """Restart recovery must not lose or duplicate an already queued slot."""

    path = tmp_path / "restart.db"
    database = Database(path)
    database.set_setting("telegram.account_id", 3)
    for index in range(1, 4):
        database.insert_channel(
            {"channel_id": index, "linked_chat_id": 100 + index, "title": f"c{index}"}
        )
    campaign = database.create_comment_campaign(
        ["text"], daily_limit=2, slot_count=2, continuous=False, account_id=3
    )
    pending = [
        row
        for row in database.get_comment_schedule(campaign["id"], limit=10)
        if row["status"] == "pending"
    ]
    due = from_db_time(pending[0]["scheduled_at"]) + timedelta(seconds=1)
    queued = database.queue_due_comment_slot(now=due)
    assert queued is not None
    database.close_thread_connection()

    reopened = Database(path)
    try:
        rows = reopened.get_comment_schedule(campaign["id"], limit=10)
        statuses = sorted(str(row["status"]) for row in rows)
        assert statuses == ["pending", "queued"]
    finally:
        reopened.close_thread_connection()


# --------------------------------------------------------------------------
# Artifact-security hardening under filesystem interference.
#
# On Windows an antivirus scanner, a backup agent or a second process can
# delete, recreate or replace the WAL/SHM sidecars between two SQLite calls.
# The main database file stays fail-closed; a sidecar that merely vanished
# must not abort a transaction.
# --------------------------------------------------------------------------


def _sidecars(database: Database) -> list[Path]:
    return [Path(f"{database.path}-wal"), Path(f"{database.path}-shm")]


def test_a_sidecar_that_vanishes_mid_check_does_not_abort_the_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core import local_security

    database = Database(tmp_path / "vanishing.db")
    try:
        database.set_setting("k", "v")
        real_validate = local_security.validate_private_regular_file

        def flaky(path, *args, **kwargs):
            target = Path(path)
            if target.name.endswith(("-wal", "-shm")):
                target.unlink(missing_ok=True)
                # Mirror the real cause chain: SQLite removing the sidecar
                # surfaces as FileNotFoundError underneath the security error.
                try:
                    raise FileNotFoundError(f"{target} disappeared")
                except FileNotFoundError as missing:
                    raise local_security.LocalFileSecurityError(
                        f"Local path is missing: {target}"
                    ) from missing
            return real_validate(path, *args, **kwargs)

        monkeypatch.setattr(
            "storage.database.validate_private_regular_file", flaky, raising=False
        )
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        assert database.get_setting("k") == "v"
    finally:
        database.close_thread_connection()


def test_an_unsafe_main_database_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database itself must never be silently skipped."""

    from core import local_security

    database = Database(tmp_path / "unsafe-main.db")
    try:
        database.set_setting("k", "v")

        def refuse(path, *args, **kwargs):
            if Path(path) == database.path:
                raise local_security.LocalFileSecurityError("Refusing symbolic-link")
            return None

        monkeypatch.setattr(
            "storage.database.validate_private_regular_file", refuse, raising=False
        )
        with pytest.raises(DatabaseError, match="Unsafe SQLite artifact"):
            database._harden_database_artifacts(force=True)  # noqa: SLF001
    finally:
        monkeypatch.undo()
        database.close_thread_connection()


def test_an_uninspectable_artifact_is_reported_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "uninspectable.db")
    try:
        database.set_setting("k", "v")
        real_exists = Path.exists

        def boom(self, *args, **kwargs):
            if self == database.path:
                raise OSError("I/O error while inspecting")
            return real_exists(self, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", boom)
        with pytest.raises(DatabaseError, match="Could not inspect SQLite artifact"):
            database._harden_database_artifacts(force=True)  # noqa: SLF001
    finally:
        monkeypatch.undo()
        database.close_thread_connection()


def test_a_recreated_sidecar_is_revalidated_rather_than_trusted(
    tmp_path: Path,
) -> None:
    """Deleting the WAL between transactions must be detected, not cached."""

    database = Database(tmp_path / "recreated.db")
    try:
        database.set_setting("k", "v1")
        for sidecar in _sidecars(database):
            sidecar.unlink(missing_ok=True)
        # A fresh transaction recreates the sidecars; hardening must accept the
        # new inode instead of failing on the remembered identity.
        database.set_setting("k", "v2")
        assert database.get_setting("k") == "v2"
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        assert database.get_setting("k") == "v2"
    finally:
        database.close_thread_connection()


def test_a_broadened_sidecar_mode_is_restored(tmp_path: Path) -> None:
    """A world-readable WAL must be tightened, not left as found."""

    import os
    import stat as stat_module

    database = Database(tmp_path / "broadened.db")
    try:
        database.set_setting("k", "v")
        wal = Path(f"{database.path}-wal")
        if not wal.exists():
            pytest.skip("WAL sidecar is not present in this configuration")
        os.chmod(wal, 0o644)
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        mode = stat_module.S_IMODE(wal.stat().st_mode)
        assert mode == 0o600, f"WAL mode was left at {oct(mode)}"
    finally:
        database.close_thread_connection()


# --------------------------------------------------------------------------
# Cleanup of the zero-byte file created by a failed construction.
# --------------------------------------------------------------------------


def test_cleanup_survives_an_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed cleanup must not mask the original construction error."""

    path = tmp_path / "marlen.db"
    real_unlink = Path.unlink

    def refuse(self, *args, **kwargs):
        if self == path:
            raise OSError("file is locked by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(DatabaseError, match="requires bootstrap"):
        Database(path, bootstrap=False)
    monkeypatch.undo()
    # The original error surfaced; the leftover is still there because the
    # filesystem refused, which is exactly what the warning records.
    assert path.exists()


def test_a_commit_failure_rolls_back_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk-full commit must surface, not be swallowed as a success."""

    from storage.sqlcipher_driver import dbapi as driver

    database = Database(tmp_path / "commit-fail.db")
    try:
        database.set_setting("keep", "before")
        real_conn = database._thread_connection()  # noqa: SLF001

        class _FailingCommit:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def commit(self):
                raise driver.OperationalError("disk I/O error")

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        database._thread_connections.connection = _FailingCommit(  # noqa: SLF001
            real_conn
        )
        with pytest.raises(DatabaseError, match="Database transaction failed"):
            with database.get_connection() as scope:
                scope.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES('keep','after')"
                )
        database._thread_connections.connection = real_conn  # noqa: SLF001
        assert database.get_setting("keep") == "before"
    finally:
        database.close_thread_connection()


def test_repeatedly_reappearing_sidecar_stops_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation must not loop forever when a sidecar keeps being recreated."""

    from core import local_security

    database = Database(tmp_path / "flapping.db")
    try:
        database.set_setting("k", "v")
        calls: list[Path] = []

        def always_missing(path, *args, **kwargs):
            target = Path(path)
            if target.name.endswith(("-wal", "-shm")):
                calls.append(target)
                target.write_bytes(b"")  # recreated between attempts
                try:
                    raise FileNotFoundError(f"{target} vanished")
                except FileNotFoundError as missing:
                    raise local_security.LocalFileSecurityError(
                        f"Local path is missing: {target}"
                    ) from missing
            return None

        monkeypatch.setattr(
            "storage.database.validate_private_regular_file",
            always_missing,
            raising=False,
        )
        with pytest.raises(DatabaseError, match="Unsafe SQLite artifact"):
            database._harden_database_artifacts(force=True)  # noqa: SLF001
        # Bounded: three attempts per sidecar, never an unbounded spin.
        assert 0 < len(calls) <= 6
    finally:
        monkeypatch.undo()
        database.close_thread_connection()


def test_an_uninspectable_sidecar_is_reported_as_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core import local_security

    database = Database(tmp_path / "sidecar-io-error.db")
    try:
        database.set_setting("k", "v")
        wal = Path(f"{database.path}-wal")

        def vanish_then_fail(path, *args, **kwargs):
            target = Path(path)
            if target == wal:
                try:
                    raise FileNotFoundError(f"{target} vanished")
                except FileNotFoundError as missing:
                    raise local_security.LocalFileSecurityError(
                        f"Local path is missing: {target}"
                    ) from missing
            return None

        real_exists = Path.exists

        def boom(self, *args, **kwargs):
            if self == wal:
                raise OSError("I/O error while inspecting the sidecar")
            return real_exists(self, *args, **kwargs)

        monkeypatch.setattr(
            "storage.database.validate_private_regular_file",
            vanish_then_fail,
            raising=False,
        )
        monkeypatch.setattr(Path, "exists", boom)
        with pytest.raises(DatabaseError, match="SQLite artifact"):
            database._harden_database_artifacts(force=True)  # noqa: SLF001
    finally:
        monkeypatch.undo()
        database.close_thread_connection()


def test_a_removed_sidecar_is_forgotten_from_the_security_cache(
    tmp_path: Path,
) -> None:
    """Stale identities must be pruned, not retained forever."""

    database = Database(tmp_path / "prune.db")
    try:
        database.set_setting("k", "v")
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        tracked_before = set(database._artifact_security_identities)  # noqa: SLF001
        database.close_thread_connection()
        for sidecar in _sidecars(database):
            sidecar.unlink(missing_ok=True)
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        tracked_after = set(database._artifact_security_identities)  # noqa: SLF001
        assert database.path in tracked_after
        assert not any(
            path.name.endswith(("-wal", "-shm")) for path in tracked_after
        ), f"removed sidecars are still tracked: {tracked_after - tracked_before}"
    finally:
        database.close_thread_connection()


def test_recovery_survives_a_corrupted_task_payload(tmp_path: Path) -> None:
    """A malformed payload must not break maintenance for every other row."""

    database = Database(tmp_path / "bad-payload.db")
    try:
        database.set_setting("telegram.account_id", 4)
        task_id = database.insert_task("noop", {})
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET payload='{not valid json' WHERE id=?", (task_id,)
            )
            conn.execute(
                """INSERT INTO direct_message_deliveries(
                       task_id, chat_id, text, status, reserved_at)
                   VALUES(?, 10, 'x', 'sending', datetime('now', '-1 hour'))""",
                (task_id,),
            )
        result = database.recover_stale_deliveries()
        assert int(result["direct_message_deliveries"]) == 1
        assert 0 in result["accounts"]
    finally:
        database.close_thread_connection()


def test_a_nonempty_file_from_a_failed_construction_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup must only ever discard a genuinely empty artifact."""

    path = tmp_path / "marlen.db"
    database = Database(path)
    database.set_setting("k", "v")
    database.close_thread_connection()
    size_before = path.stat().st_size

    from storage import database as database_module

    def explode(self, *, bootstrap: bool) -> None:
        raise RuntimeError("injected failure after the file already had content")

    monkeypatch.setattr(database_module.Database, "_init_schema", explode)
    with pytest.raises(RuntimeError, match="injected failure"):
        Database(path)
    monkeypatch.undo()

    assert path.exists() and path.stat().st_size == size_before
    reopened = Database(path)
    try:
        assert reopened.get_setting("k") == "v"
    finally:
        reopened.close_thread_connection()


# --------------------------------------------------------------------------
# The periodic cheap re-check between full hardening passes.
# --------------------------------------------------------------------------


def test_the_cheap_recheck_is_a_no_op_for_unchanged_artifacts(
    tmp_path: Path,
) -> None:
    """A second non-forced pass inside the window must not re-validate."""

    database = Database(tmp_path / "cheap-pass.db")
    try:
        database.set_setting("k", "v")
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        before = dict(database._artifact_security_identities)  # noqa: SLF001
        database._harden_database_artifacts(force=False)  # noqa: SLF001
        assert dict(database._artifact_security_identities) == before  # noqa: SLF001
    finally:
        database.close_thread_connection()


def test_the_cheap_recheck_detects_a_replaced_database_file(
    tmp_path: Path,
) -> None:
    """Swapping the file on disk must force a full re-validation."""

    database = Database(tmp_path / "replaced.db")
    try:
        database.set_setting("k", "v")
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        tracked = dict(database._artifact_security_identities)  # noqa: SLF001
        assert database.path in tracked

        # Replace the database with a different inode while keeping it private.
        import os

        payload = database.path.read_bytes()
        database.close_thread_connection()
        replacement = database.path.with_name("replacement.tmp")
        replacement.write_bytes(payload)
        os.chmod(replacement, 0o600)
        os.replace(replacement, database.path)

        database._harden_database_artifacts(force=False)  # noqa: SLF001
        assert (
            database._artifact_security_identities.get(database.path)  # noqa: SLF001
            != tracked[database.path]
        )
    finally:
        database.close_thread_connection()


def test_the_cheap_recheck_survives_an_lstat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable artifact must trigger a full pass, not a crash."""

    database = Database(tmp_path / "cheap-lstat.db")
    try:
        database.set_setting("k", "v")
        database._harden_database_artifacts(force=True)  # noqa: SLF001

        real_lstat = Path.lstat
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            if self == database.path and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("transient I/O error")
            return real_lstat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", flaky)
        database._harden_database_artifacts(force=False)  # noqa: SLF001
        monkeypatch.undo()
        assert calls["n"] == 1
        assert database.get_setting("k") == "v"
    finally:
        monkeypatch.undo()
        database.close_thread_connection()


def test_a_broadened_database_mode_is_detected_by_the_cheap_pass(
    tmp_path: Path,
) -> None:
    """A world-readable database must be tightened on the next check."""

    import os
    import stat as stat_module

    database = Database(tmp_path / "broadened-main.db")
    try:
        database.set_setting("k", "v")
        database._harden_database_artifacts(force=True)  # noqa: SLF001
        os.chmod(database.path, 0o644)
        database._harden_database_artifacts(force=False)  # noqa: SLF001
        mode = stat_module.S_IMODE(database.path.stat().st_mode)
        assert mode == 0o600, f"database mode was left at {oct(mode)}"
    finally:
        database.close_thread_connection()
