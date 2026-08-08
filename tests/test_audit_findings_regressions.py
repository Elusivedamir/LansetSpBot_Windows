from __future__ import annotations

from pathlib import Path

from services.api_parts.accounts import AccountsAPIMixin
from services.api_parts.task_queue import TaskQueueAPIMixin


class _ScopedDailyDatabase:
    def __init__(self, root, account_id: int):
        self.root = root
        self.account_id = int(account_id)

    def get_setting(self, key, default=None):
        return self.root.values.get((self.account_id, key), default)

    def set_setting(self, key, value):
        self.root.values[(self.account_id, key)] = value

    def get_active_comment_campaign(self, *, account_id):
        assert int(account_id) == self.account_id
        return None


class _DailyDatabase:
    def __init__(self):
        self.values = {}

    def for_account(self, account_id):
        return _ScopedDailyDatabase(self, int(account_id))


class _DailyLimitAPI(TaskQueueAPIMixin):
    COMMENT_DAILY_LIMIT_SETTING = "commenting.daily_limit"
    max_channels_per_run = 40

    def __init__(self):
        self.database = _DailyDatabase()
        self.current_account_id = 2

    def get_current_account_id(self):
        return self.current_account_id


def test_daily_limit_explicit_owner_does_not_follow_selected_account():
    api = _DailyLimitAPI()
    api.database.values[(1, api.COMMENT_DAILY_LIMIT_SETTING)] = 200
    api.database.values[(2, api.COMMENT_DAILY_LIMIT_SETTING)] = 40

    assert api.get_comment_daily_limit(account_id=1) == 200
    api.current_account_id = 2
    api.set_comment_daily_limit(175, account_id=1)

    assert api.database.values[(1, api.COMMENT_DAILY_LIMIT_SETTING)] == 175
    assert api.database.values[(2, api.COMMENT_DAILY_LIMIT_SETTING)] == 40


class _TransferDatabase:
    def __init__(self):
        self.accounts = {1: {"id": 1}, 2: {"id": 2}, 3: {"id": 3}, 4: {"id": 4}}
        self.comment_calls = []
        self.channel_calls = []

    def get_telegram_account(self, account_id):
        return self.accounts.get(int(account_id))

    def import_comment_profile_between_accounts(
        self, *, source_account_id, target_account_id, mode
    ):
        self.comment_calls.append(
            (int(source_account_id), int(target_account_id), str(mode))
        )
        return {"imported": 1}

    def import_channels_between_accounts(
        self, *, source_account_id, target_account_id
    ):
        self.channel_calls.append((int(source_account_id), int(target_account_id)))
        return {"imported": 1, "existing": 0, "skipped": 0}


class _TransferAPI(AccountsAPIMixin):
    def __init__(self):
        self.database = _TransferDatabase()
        self.selected = 4
        self.previous = 3

    def get_selected_account_id(self):
        return self.selected

    def get_previous_selected_account_id(self):
        return self.previous


def test_import_uses_captured_ids_not_later_selected_account():
    api = _TransferAPI()
    api.import_comments_from_previous_account(
        mode="replace", source_account_id=1, target_account_id=2
    )
    api.import_channels_from_previous_account(
        source_account_id=1, target_account_id=2
    )

    assert api.database.comment_calls == [(1, 2, "replace")]
    assert api.database.channel_calls == [(1, 2)]


def test_release_bundle_has_fail_closed_dev_dependency_guard():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "build" / "LansetSpBot.windows.spec").read_text(encoding="utf-8")
    build = (root / "build" / "build_windows_x64.ps1").read_text(encoding="utf-8")

    assert '"mypy"' in spec
    assert '"setuptools"' in spec
    assert "Assert-NoDevelopmentPackages" in build
    assert "Assert-NoDevelopmentPackages -BundleRoot $BuiltDir" in build
