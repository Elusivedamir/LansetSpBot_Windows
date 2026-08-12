from __future__ import annotations

import threading
from typing import Any

import pytest

from services.api_parts.warmup import WarmupAPIMixin


class _DB:
    def __init__(self) -> None:
        self.acquire_calls: list[tuple[int, str, dict[str, Any]]] = []
        self.release_calls: list[tuple[int, str]] = []
        self.enqueue_calls: list[int] = []
        self.fail_acquire_account: int | None = None
        self.create_error: BaseException | None = None
        self.extend_error: BaseException | None = None
        self.pair: dict[str, Any] = {
            "id": 7,
            "status": "completed",
            "week_number": 1,
            "account_a_id": 101,
            "account_b_id": 102,
            "owner_token_a": "a" * 32,
            "owner_token_b": "b" * 32,
        }
        self.lease: dict[str, Any] | None = None
        self.active_pairs: list[dict[str, Any]] = []
        self.recovered = 0
        self.paused: list[tuple[int, str | None]] = []
        self.resume_changed = True
        self.retry_changed = True
        self.groups: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.pairs: list[dict[str, Any]] = []
        self.activity: list[dict[str, Any]] = []
        self.saved_dialogs: list[dict[str, Any]] = []
        self.added_groups: list[tuple[str, str]] = []
        self.assigned_groups: list[tuple[int, int, str]] = []

    def acquire_account_activity_lease(
        self,
        account_id: int,
        *,
        owner_token: str,
        lease_seconds: int,
        metadata: dict[str, Any],
    ) -> None:
        assert lease_seconds == 30 * 60
        if self.fail_acquire_account == account_id:
            raise RuntimeError("lease conflict")
        self.acquire_calls.append((account_id, owner_token, dict(metadata)))

    def release_account_activity_lease(
        self, account_id: int, *, owner_token: str
    ) -> None:
        self.release_calls.append((account_id, owner_token))

    def create_warmup_pair(self, **kwargs):
        if self.create_error is not None:
            raise self.create_error
        self.create_kwargs = dict(kwargs)
        return {"id": 7, "status": "running"}

    def extend_warmup_pair(self, pair_id: int, **kwargs):
        if self.extend_error is not None:
            raise self.extend_error
        self.extend_kwargs = {"pair_id": pair_id, **kwargs}
        return {"id": pair_id, "status": "running", "week_number": 2}

    def enqueue_warmup_step(self, pair_id: int):
        self.enqueue_calls.append(pair_id)
        return {"pair_id": pair_id}

    def get_warmup_pair(self, _pair_id: int):
        return dict(self.pair)

    def get_account_activity_lease(self, _account_id: int):
        return dict(self.lease) if self.lease else None

    def pause_warmup_pair(self, pair_id: int, reason: str | None = None):
        self.paused.append((pair_id, reason))
        return True

    def resume_warmup_pair(self, _pair_id: int):
        return self.resume_changed

    def retry_failed_warmup_step(self, _pair_id: int):
        return self.retry_changed

    def transfer_warmup_account(self, account_id: int):
        return {"account_id": account_id, "status": "transferred"}

    def recover_stale_warmup_steps(self) -> int:
        self.recovered += 1
        return 0

    def list_active_warmup_pairs(self):
        return [dict(item) for item in self.active_pairs]

    def list_warmup_account_states(self):
        return [dict(item) for item in self.states]

    def list_warmup_pairs(self):
        return [dict(item) for item in self.pairs]

    def list_warmup_pair_activity(self):
        return [dict(item) for item in self.activity]

    def list_warmup_groups(self):
        return [dict(item) for item in self.groups]

    def get_saved_dialogs(self, account_id=None):
        return [dict(item) for item in self.saved_dialogs]

    def add_warmup_group(self, chat_ref: str, title: str):
        self.added_groups.append((chat_ref, title))
        return {
            "id": len(self.added_groups),
            "chat_ref": chat_ref,
            "title": title,
        }

    def remove_warmup_group(self, group_id: int):
        return group_id == 1

    def assign_warmup_group_to_account(
        self, group_id: int, account_id: int, *, membership_state: str
    ) -> None:
        self.assigned_groups.append((group_id, account_id, membership_state))

    def remove_warmup_group_from_account(
        self, group_id: int, account_id: int
    ) -> bool:
        return group_id == 1 and account_id == 101


class _Worker:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.cancelled: list[tuple[tuple[str, int], ...]] = []
        self.cleared: list[tuple[str, int]] = []

    def isRunning(self) -> bool:
        return self.running

    def cancel_scopes_and_run(self, scopes, mutation):
        self.cancelled.append(tuple(scopes))
        return mutation()

    def clear_scope_cancellation(self, scope: str, owner: int) -> None:
        self.cleared.append((scope, owner))


class _Host(WarmupAPIMixin):
    def __init__(self) -> None:
        self.database = _DB()
        self.queue_worker: _Worker | None = None
        self._secret_lock = threading.RLock()
        self._shutdown_requested = False
        self._warmup_recovery_done = False
        self.queue_starts = 0
        self._phones = {101: "+79990000001", 102: "+79990000002"}
        self._settings: dict[int, dict[str, object]] = {
            101: self._valid_proxy("proxy-a.example", "1080"),
            102: self._valid_proxy("proxy-b.example", "1081"),
        }
        self._accounts: list[dict[str, Any]] = []

    @staticmethod
    def _valid_proxy(host: str, port: str) -> dict[str, object]:
        return {
            "telegram.proxy_enabled": "1",
            "telegram.proxy_host": host,
            "telegram.proxy_port": port,
            "telegram.proxy_type": "SOCKS5",
            "telegram.proxy_username": "operator",
        }

    def get_account_settings(self, account_id: int) -> dict[str, object]:
        return dict(self._settings.get(account_id, {}))

    def _strict_account_secret(self, account_id: int, key: str):
        assert key == "telegram.phone"
        return self._phones.get(account_id)

    def start_queue(self) -> None:
        self.queue_starts += 1

    def list_telegram_accounts(self):
        return [dict(item) for item in self._accounts]


def test_warmup_selector_snapshot_does_not_read_proxy_secrets() -> None:
    host = _Host()
    host._accounts = [
        {
            "telegram_account_id": 101,
            "display_name": "A",
            "authorized": True,
            "stopped": False,
        },
        {
            "telegram_account_id": 102,
            "display_name": "B",
            "authorized": False,
            "stopped": True,
        },
    ]
    host.database.states = [
        {
            "account_id": 101,
            "status": "active",
            "active_pair_id": 7,
            "weeks_completed": 0,
        }
    ]

    def forbidden_proxy_read(_account_id: int):
        raise AssertionError("selector snapshot must not read proxy/secret settings")

    host._warmup_proxy_summary = forbidden_proxy_read  # type: ignore[method-assign]

    rows = host.get_warmup_selector_accounts()

    assert [int(item["telegram_account_id"]) for item in rows] == [101, 102]
    assert rows[0]["warmup_status"] == "active"
    assert int(rows[0]["active_pair_id"]) == 7
    assert rows[1]["warmup_status"] == "available"
    assert rows[1]["active_pair_id"] is None


def test_proxy_summary_rejects_invalid_and_out_of_range_ports() -> None:
    host = _Host()

    valid = host._warmup_proxy_summary(101)
    assert valid["configured"] is True
    assert valid["type"] == "SOCKS5"
    assert valid["host_masked"] != "proxy-a.example"
    assert valid["username_masked"] != "operator"

    host._settings[101]["telegram.proxy_port"] = "65536"
    assert host._warmup_proxy_summary(101)["configured"] is False

    host._settings[101]["telegram.proxy_port"] = "9" * 5000
    assert host._warmup_proxy_summary(101)["configured"] is False

    host._settings[101]["telegram.proxy_port"] = "not-a-port"
    assert host._warmup_proxy_summary(101)["configured"] is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_setting_enabled_accepts_supported_truthy_values(value: str) -> None:
    assert _Host._setting_enabled(value) is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", None])
def test_setting_enabled_rejects_other_values(value: object) -> None:
    assert _Host._setting_enabled(value) is False


def test_group_reference_normalization_accepts_telegram_only() -> None:
    normalize = _Host._normalize_group_ref
    assert normalize("@group_name") == "@group_name"
    assert normalize("group_name") == "@group_name"
    assert normalize("t.me/group_name") == "https://t.me/group_name"
    assert normalize("https://telegram.me/+invite") == "https://telegram.me/+invite"

    with pytest.raises(ValueError):
        normalize("")
    with pytest.raises(ValueError):
        normalize("@")
    with pytest.raises(ValueError):
        normalize("https://example.com/group")
    with pytest.raises(ValueError):
        normalize("https://t.me/")
    with pytest.raises(ValueError):
        normalize("bad group!")


def test_populate_warmup_groups_from_synced_selects_three_or_four_unique_groups() -> None:
    host = _Host()
    host.database.saved_dialogs = [
        {
            "peer_id": -1001,
            "title": "Alpha",
            "username": "alpha_group",
            "kind": "supergroup",
            "membership_status": "member",
        },
        {
            "peer_id": -1002,
            "title": "Beta",
            "username": "beta_group",
            "kind": "group",
            "membership_status": "member",
        },
        {
            "peer_id": -1003,
            "title": "Gamma",
            "invite_link": "https://t.me/+GammaInvite",
            "kind": "group",
            "membership_status": "member",
        },
        {
            "peer_id": -1004,
            "title": "Delta",
            "username": "delta_group",
            "kind": "supergroup",
            "membership_status": "member",
        },
        {
            "peer_id": -1005,
            "title": "Epsilon",
            "username": "epsilon_group",
            "kind": "group",
            "membership_status": "member",
        },
        {
            "peer_id": -2001,
            "title": "Broadcast",
            "username": "broadcast_channel",
            "kind": "channel",
            "membership_status": "member",
        },
        {
            "peer_id": -2002,
            "title": "Left group",
            "username": "left_group",
            "kind": "group",
            "membership_status": "left",
        },
        {
            "peer_id": -2003,
            "title": "Private without locator",
            "kind": "group",
            "membership_status": "member",
        },
    ]

    result = host.populate_warmup_groups_from_synced(101)

    assert result["candidate_count"] == 6
    assert 3 <= result["selected_count"] <= 4
    assert result["limited"] is False
    assert result["message"] == ""
    assert len(host.database.added_groups) == result["selected_count"]
    assert host.database.assigned_groups == [
        (group_id, 101, "joined")
        for group_id in range(1, result["selected_count"] + 1)
    ]
    refs = [item[0] for item in host.database.added_groups]
    assert len(refs) == len(set(refs))
    assert set(refs) <= {
        "@alpha_group",
        "@beta_group",
        "https://t.me/+GammaInvite",
        "@delta_group",
        "@epsilon_group",
        "@broadcast_channel",
    }


def test_populate_warmup_groups_keeps_one_or_two_available_targets() -> None:
    host = _Host()
    host.database.saved_dialogs = [
        {
            "title": "One",
            "username": "one_group",
            "kind": "group",
            "membership_status": "member",
        },
        {
            "title": "Two",
            "username": "two_group",
            "kind": "supergroup",
            "membership_status": "member",
        },
    ]

    result = host.populate_warmup_groups_from_synced(101)

    assert result["candidate_count"] == 2
    assert result["selected_count"] == 2
    assert result["limited"] is True
    assert result["message"] == ""
    assert {item[0] for item in host.database.added_groups} == {
        "@one_group",
        "@two_group",
    }


def test_populate_warmup_groups_returns_status_instead_of_throwing_when_empty() -> None:
    host = _Host()
    host.database.saved_dialogs = [
        {
            "title": "Private without locator",
            "kind": "group",
            "membership_status": "member",
        },
        {
            "title": "Left public channel",
            "username": "left_public",
            "kind": "channel",
            "membership_status": "left",
        },
    ]

    result = host.populate_warmup_groups_from_synced(101)

    assert result["candidate_count"] == 0
    assert result["selected_count"] == 0
    assert result["limited"] is True
    assert "не найдено доступных" in result["message"]
    assert host.database.added_groups == []


def test_pair_lease_acquisition_rolls_back_partial_success() -> None:
    host = _Host()
    host.database.fail_acquire_account = 102

    with pytest.raises(RuntimeError, match="lease conflict"):
        host._acquire_pair_leases(
            account_a_id=101,
            account_b_id=102,
            owner_token_a="a",
            owner_token_b="b",
        )

    assert host.database.release_calls == [(101, "a")]


def test_require_warmup_account_allows_missing_proxy_but_requires_phone() -> None:
    host = _Host()
    host._settings[101]["telegram.proxy_enabled"] = "0"
    host._require_warmup_account(101)

    host._phones[101] = ""
    with pytest.raises(ValueError):
        host._require_warmup_account(101)

def test_create_and_extend_pair_schedule_steps_and_queue() -> None:
    host = _Host()

    created = host.create_warmup_pair(101, 102)
    assert created["pair"]["id"] == 7
    assert host.database.enqueue_calls == [7]
    assert host.queue_starts == 1
    assert len(host.database.create_kwargs["steps"]) > 70
    assert len(host.database.acquire_calls) == 2

    host.database.acquire_calls.clear()
    extended = host.extend_warmup_pair(7)
    assert extended["pair"]["week_number"] == 2
    assert host.database.enqueue_calls == [7, 7]
    assert host.queue_starts == 2
    assert host.database.extend_kwargs["pair_id"] == 7
    assert len(host.database.extend_kwargs["steps"]) > 70


def test_create_pair_releases_leases_if_repository_create_fails() -> None:
    host = _Host()
    host.database.create_error = RuntimeError("write failed")

    with pytest.raises(RuntimeError, match="write failed"):
        host.create_warmup_pair(101, 102)

    released_ids = [account_id for account_id, _token in host.database.release_calls]
    assert released_ids == [101, 102]


def test_pause_resume_retry_and_transfer_lifecycle() -> None:
    host = _Host()
    worker = _Worker()
    host.queue_worker = worker

    assert host.pause_warmup_pair(7) is True
    assert worker.cancelled == [(("warmup_pair", 7),)]

    assert host.resume_warmup_pair(7) is True
    assert worker.cleared[-1] == ("warmup_pair", 7)
    assert host.database.enqueue_calls[-1] == 7

    assert host.retry_failed_warmup_pair(7) is True
    assert worker.cleared[-1] == ("warmup_pair", 7)
    assert host.database.enqueue_calls[-1] == 7

    host.database.lease = {"owner_token": "lease-token"}
    result = host.transfer_warmup_account(101)
    assert result["status"] == "transferred"
    assert host.database.release_calls[-1] == (101, "lease-token")


def test_resume_and_retry_noop_do_not_queue() -> None:
    host = _Host()
    host.database.resume_changed = False
    host.database.retry_changed = False

    assert host.resume_warmup_pair(7) is False
    assert host.retry_failed_warmup_pair(7) is False
    assert host.database.enqueue_calls == []
    assert host.queue_starts == 0


def test_resume_uses_safe_failed_step_retry() -> None:
    host = _Host()
    host.database.resume_changed = False
    host.database.retry_changed = True

    assert host.resume_warmup_pair(7) is True
    assert host.database.enqueue_calls == [7]
    assert host.queue_starts == 1


def test_overview_marks_only_proxy_ready_available_accounts_eligible() -> None:
    host = _Host()
    host._accounts = [
        {
            "telegram_account_id": 101,
            "authorized": True,
            "stopped": False,
            "campaign_active": False,
        },
        {
            "telegram_account_id": 102,
            "authorized": True,
            "stopped": False,
            "campaign_active": False,
        },
    ]
    host.database.states = [
        {
            "account_id": 101,
            "status": "available",
            "active_pair_id": None,
            "weeks_completed": 1,
        },
        {
            "account_id": 102,
            "status": "active",
            "active_pair_id": 7,
            "weeks_completed": 0,
        },
    ]
    host.database.pairs = [
        {
            "id": 7,
            "day_order": "support,work",
            "finished_steps": 5,
            "total_steps": 10,
        }
    ]
    host.database.activity = [
        {
            "pair_id": 7,
            "snapshot_kind": "focus",
            "sequence_no": 6,
            "action": "message",
            "status": "pending",
        }
    ]

    overview = host.get_warmup_overview()

    assert overview["accounts"][0]["warmup_eligible"] is True
    assert overview["accounts"][1]["warmup_eligible"] is False
    assert overview["pairs"][0]["progress_percent"] == 50
    assert overview["pairs"][0]["activity"]["focus"]["action"] == "message"
    assert overview["active_account_count"] == 1
    assert overview["account_limit"] == 40


def test_bootstrap_recovers_once_and_lease_tick_queues_running_pairs() -> None:
    host = _Host()
    host.database.active_pairs = [
        {
            "id": 7,
            "status": "running",
            "account_a_id": 101,
            "account_b_id": 102,
            "owner_token_a": "a",
            "owner_token_b": "b",
        }
    ]

    host._warmup_bootstrap()
    host._warmup_bootstrap()

    assert host.database.recovered == 1
    assert host.database.enqueue_calls == [7, 7]
    assert host.queue_starts == 2
    assert len(host.database.acquire_calls) == 4


def test_add_and_remove_group_delegate_normalized_values() -> None:
    host = _Host()
    group = host.add_warmup_group("t.me/group_name", 101)
    assert group["chat_ref"] == "https://t.me/group_name"
    assert host.database.assigned_groups == [(1, 101, "unknown")]
    assert host.remove_warmup_group(1, 101) is True
    assert host.remove_warmup_group(2, 101) is False
