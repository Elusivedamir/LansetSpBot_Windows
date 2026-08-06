from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import timedelta

import pytest

from core.campaign_schedule import to_db_time, utc_now
from storage.db_account_activity import (
    CAMPAIGN_WARMUP_CONFLICT_MESSAGE,
    WARMUP_ALREADY_RUNNING_MESSAGE,
    WARMUP_CAMPAIGN_CONFLICT_MESSAGE,
    AccountActivityRepositoryMixin,
)
from storage.db_common import DatabaseError


class LeaseHarness(AccountActivityRepositoryMixin):
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE account_activity_leases(
                account_id INTEGER PRIMARY KEY,
                activity TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                started_at DATETIME NOT NULL,
                heartbeat_at DATETIME NOT NULL,
                lease_until DATETIME NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE comment_campaigns(
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE join_campaigns(
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            """
        )

    @contextmanager
    def get_connection(self):
        try:
            yield self.connection
            if self.connection.in_transaction:
                self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise


def test_warmup_lease_blocks_campaign_with_exact_popup_text() -> None:
    db = LeaseHarness()
    db.acquire_account_activity_lease(100, owner_token="a" * 32)

    with pytest.raises(DatabaseError, match="прогреве") as caught:
        db.require_account_not_warming(100)

    assert str(caught.value) == WARMUP_CAMPAIGN_CONFLICT_MESSAGE


def test_active_comment_campaign_blocks_warmup() -> None:
    db = LeaseHarness()
    db.connection.execute(
        "INSERT INTO comment_campaigns(id, account_id, status) VALUES(1, 100, 'paused')"
    )

    with pytest.raises(DatabaseError) as caught:
        db.acquire_account_activity_lease(100, owner_token="a" * 32)

    assert str(caught.value) == CAMPAIGN_WARMUP_CONFLICT_MESSAGE


def test_active_join_campaign_blocks_warmup() -> None:
    db = LeaseHarness()
    db.connection.execute(
        "INSERT INTO join_campaigns(id, account_id, status) VALUES(1, 100, 'network_wait')"
    )

    with pytest.raises(DatabaseError) as caught:
        db.acquire_account_activity_lease(100, owner_token="a" * 32)

    assert str(caught.value) == CAMPAIGN_WARMUP_CONFLICT_MESSAGE


def test_second_owner_cannot_take_live_lease() -> None:
    db = LeaseHarness()
    db.acquire_account_activity_lease(100, owner_token="a" * 32)

    with pytest.raises(DatabaseError) as caught:
        db.acquire_account_activity_lease(100, owner_token="b" * 32)

    assert str(caught.value) == WARMUP_ALREADY_RUNNING_MESSAGE


def test_stale_lease_is_removed_and_can_be_reacquired() -> None:
    db = LeaseHarness()
    old = utc_now() - timedelta(hours=2)
    db.connection.execute(
        """INSERT INTO account_activity_leases(
               account_id, activity, owner_token, started_at,
               heartbeat_at, lease_until, metadata_json)
           VALUES(100, 'warmup', ?, ?, ?, ?, '{}')""",
        (
            "a" * 32,
            to_db_time(old),
            to_db_time(old),
            to_db_time(old + timedelta(minutes=30)),
        ),
    )

    lease = db.acquire_account_activity_lease(100, owner_token="b" * 32)

    assert lease["owner_token"] == "b" * 32


def test_only_owner_can_renew_or_release() -> None:
    db = LeaseHarness()
    db.acquire_account_activity_lease(100, owner_token="a" * 32)

    assert not db.renew_account_activity_lease(100, owner_token="b" * 32)
    assert not db.release_account_activity_lease(100, owner_token="b" * 32)
    assert db.renew_account_activity_lease(100, owner_token="a" * 32)
    assert db.release_account_activity_lease(100, owner_token="a" * 32)
    assert db.get_account_activity_lease(100) is None


def _initialize_file_database(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE account_activity_leases(
            account_id INTEGER PRIMARY KEY,
            activity TEXT NOT NULL,
            owner_token TEXT NOT NULL,
            started_at DATETIME NOT NULL,
            heartbeat_at DATETIME NOT NULL,
            lease_until DATETIME NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE comment_campaigns(
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE join_campaigns(
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
    conn.close()


class FileLeaseHarness(AccountActivityRepositoryMixin):
    def __init__(self, path) -> None:
        self.path = str(path)

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            if conn.in_transaction:
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def test_parallel_warmup_starts_have_one_owner(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "leases.db"
    _initialize_file_database(path)
    barrier = Barrier(2)

    def acquire(token: str) -> str:
        db = FileLeaseHarness(path)
        barrier.wait()
        try:
            db.acquire_account_activity_lease(100, owner_token=token)
        except DatabaseError as exc:
            return str(exc)
        return "acquired"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(acquire, ("a" * 32, "b" * 32)))

    assert outcomes.count("acquired") == 1
    assert outcomes.count(WARMUP_ALREADY_RUNNING_MESSAGE) == 1


def test_campaign_and_warmup_race_has_one_winner(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from storage.db_account_activity import (
        get_active_account_activity_lease_in_transaction,
    )

    path = tmp_path / "campaign-race.db"
    _initialize_file_database(path)
    barrier = Barrier(2)

    def start_warmup() -> str:
        db = FileLeaseHarness(path)
        barrier.wait()
        try:
            db.acquire_account_activity_lease(100, owner_token="a" * 32)
        except DatabaseError:
            return "blocked"
        return "warmup"

    def start_campaign() -> str:
        barrier.wait()
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            if get_active_account_activity_lease_in_transaction(conn, 100):
                conn.execute("ROLLBACK")
                return "blocked"
            conn.execute(
                "INSERT INTO comment_campaigns(id, account_id, status) "
                "VALUES(1, 100, 'running')"
            )
            conn.execute("COMMIT")
            return "campaign"
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        warmup_future = pool.submit(start_warmup)
        campaign_future = pool.submit(start_campaign)
        outcomes = {warmup_future.result(), campaign_future.result()}

    assert outcomes in ({"warmup", "blocked"}, {"campaign", "blocked"})


def test_lease_rejects_oversized_identity_and_metadata() -> None:
    db = LeaseHarness()
    with pytest.raises(ValueError, match="64-bit"):
        db.acquire_account_activity_lease(
            9_223_372_036_854_775_808, owner_token="a" * 32
        )
    with pytest.raises(DatabaseError, match="too large"):
        db.acquire_account_activity_lease(
            100,
            owner_token="a" * 32,
            metadata={"value": "x" * 5000},
        )
