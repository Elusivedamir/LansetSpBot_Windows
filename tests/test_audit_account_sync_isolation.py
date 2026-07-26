from __future__ import annotations

import pytest

from core.exceptions import NonRetryableTelegramError
from storage.database import Database
from tests.test_composition_resilience import _Telegram, _handlers


class _SwitchingTelegram(_Telegram):
    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database

    async def iter_dialog_snapshot(self):
        for index in range(1, 202):
            if index == 201:
                self.database.set_setting("telegram.account_id", 88)
            yield {
                "work_target": {
                    "id": index,
                    "title": f"Channel {index}",
                    "username": f"channel_{index}",
                    "target_kind": "channel",
                    "comment_mode": "channel_post",
                },
                "saved_dialog": None,
            }


@pytest.mark.asyncio
async def test_sync_does_not_write_next_batch_into_switched_account(
    monkeypatch, tmp_path
):
    database = Database(tmp_path / "sync-account-switch.db")
    database.set_setting("telegram.account_id", 77)
    database.set_setting("telegram.phone", "+100")
    telegram = _SwitchingTelegram(database)
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, database, telegram
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        await handlers["sync_channels"](
            {"id": 1, "payload": {"account_id": 77}}
        )

    assert raised.value.code == "account_state_mismatch"
    assert len(database.get_channels(account_id=77)) == 200
    assert database.get_channels(account_id=88) == []


class _SwitchingLinked:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get_linked_chat_id(self, channel_id):
        del channel_id
        self.database.set_setting("telegram.account_id", 88)
        return None


@pytest.mark.asyncio
async def test_link_result_is_persisted_only_to_task_account_after_switch(
    monkeypatch, tmp_path
):
    database = Database(tmp_path / "link-account-switch.db")
    database.set_setting("telegram.account_id", 77)
    database.set_setting("telegram.phone", "+100")
    row = {
        "channel_id": 100,
        "title": "Same peer",
        "username": "same_peer",
        "target_kind": "channel",
        "comment_mode": "channel_post",
    }
    database.upsert_channels_batch([row], account_id=77)
    database.upsert_channels_batch([row], account_id=88)
    task_id = database.insert_task("link_channels", {"account_id": 77})
    claimed = database.claim_next_pending_task()
    assert claimed and int(claimed["id"]) == task_id

    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch,
        database,
        _Telegram(),
        linked=_SwitchingLinked(database),
    )

    with pytest.raises(NonRetryableTelegramError) as raised:
        await handlers["link_channels"](
            {"id": task_id, "payload": {"account_id": 77}, "progress": 0}
        )

    assert raised.value.code == "account_state_mismatch"
    source = database.get_channel_by_id(100, account_id=77)
    switched = database.get_channel_by_id(100, account_id=88)
    assert source is not None and switched is not None
    assert source["link_status"] == "Нет чата обсуждения"
    assert switched["link_status"] is None
    assert switched["link_checked_at"] is None
