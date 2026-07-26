from __future__ import annotations

import copy
import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from core.campaign_schedule import to_db_time, utc_now
from core.exceptions import DeferredTelegramError
from gui.views.links_view import LinksView
from storage.database import Database
from tests.test_composition_resilience import _Linked, _Telegram, _handlers


class _DeferredLinked(_Linked):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[int] = []
        self.defer_channel_id: int | None = 2

    async def get_linked_chat_id(self, channel_id):
        numeric = int(channel_id)
        self.calls.append(numeric)
        if self.defer_channel_id == numeric:
            self.defer_channel_id = None
            raise DeferredTelegramError(
                "Telegram FloodWait",
                code="flood_wait_deferred",
                retry_after=120,
            )
        return self.links.get(numeric)


@pytest.mark.asyncio
async def test_link_channels_skips_floodwait_channel_and_resumes_from_next(monkeypatch):
    database = MagicMock()
    database.get_setting.return_value = 77
    database.get_channels.return_value = [
        {"channel_id": 1, "title": "One", "target_kind": "channel"},
        {"channel_id": 2, "title": "Two", "target_kind": "channel"},
        {"channel_id": 3, "title": "Three", "target_kind": "channel"},
    ]
    linked = _DeferredLinked()
    linked.links = {1: None, 2: None, 3: None}
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, database, _Telegram(), linked=linked
    )

    with pytest.raises(DeferredTelegramError):
        await handlers["link_channels"]({"id": 41, "payload": {"account_id": 77}})

    assert linked.calls == [1, 2]
    checkpoint_payload = copy.deepcopy(
        database.update_task_checkpoint.call_args_list[-1].args[1]
    )
    checkpoint = checkpoint_payload["_link_checkpoint"]
    assert checkpoint["channel_index"] == 2
    assert checkpoint["group_index"] == 0
    database.mark_link_checked.assert_any_call(2, account_id=77)
    assert any(
        call.args == (2, None, None, "Пропущено · Telegram FloodWait")
        for call in database.update_channel_link.call_args_list
    )

    linked.calls.clear()
    await handlers["link_channels"]({"id": 41, "payload": checkpoint_payload})

    assert linked.calls == [3]
    final_payload = database.update_task_checkpoint.call_args_list[-1].args[1]
    assert "_link_checkpoint" not in final_payload
    database.update_task_progress.assert_called_with(41, 100)


@pytest.mark.asyncio
async def test_legacy_deferred_task_uses_saved_progress_as_initial_cursor(monkeypatch):
    database = MagicMock()
    database.get_setting.return_value = 77
    database.get_channels.return_value = [
        {
            "channel_id": 1,
            "title": "One",
            "target_kind": "channel",
            "link_status": "Нет чата обсуждения",
        },
        {
            "channel_id": 2,
            "title": "Two",
            "target_kind": "channel",
            "link_status": "Нет чата обсуждения",
        },
        {"channel_id": 3, "title": "Three", "target_kind": "channel"},
        {"channel_id": 4, "title": "Four", "target_kind": "channel"},
    ]
    linked = _DeferredLinked()
    linked.defer_channel_id = None
    linked.links = {1: None, 2: None, 3: None, 4: None}
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, database, _Telegram(), linked=linked
    )

    await handlers["link_channels"](
        {
            "id": 43,
            "payload": {"account_id": 77},
            "progress": 50,
            "defer_count": 1,
        }
    )

    assert linked.calls == [3, 4]


@pytest.mark.asyncio
async def test_link_channels_waits_between_all_target_checks(monkeypatch):
    database = MagicMock()
    database.get_setting.return_value = 77
    database.get_channels.return_value = [
        {"channel_id": 1, "title": "One", "target_kind": "channel"},
        {"channel_id": 2, "title": "Two", "target_kind": "channel"},
        {"channel_id": 3, "title": "Three", "target_kind": "channel"},
    ]
    linked = _Linked()
    linked.links = {1: None, 2: None, 3: None}
    handlers, _cleanup, _comments, worker = _handlers(
        monkeypatch,
        database,
        _Telegram(),
        linked=linked,
        link_check_delay_min=3,
        link_check_delay_max=7,
    )

    await handlers["link_channels"]({"id": 42, "payload": {"account_id": 77}})

    assert len(worker.sleep_calls) == 2
    assert all(3 <= delay <= 7 for delay in worker.sleep_calls)


def test_task_checkpoint_payload_and_progress_are_atomic(tmp_path):
    database = Database(tmp_path / "checkpoint.db")
    task_id = database.insert_task("link_channels", {"account_id": 77})
    claimed = database.claim_next_pending_task()
    assert claimed and claimed["id"] == task_id

    payload = {
        "account_id": 77,
        "_link_checkpoint": {"version": 1, "channel_index": 4},
    }
    assert database.update_task_checkpoint(task_id, payload, 40)

    stored = database.get_task(task_id)
    assert stored is not None
    assert stored["progress"] == 40
    assert json.loads(stored["payload"]) == payload


def test_links_view_shows_floodwait_countdown():
    app = QApplication.instance() or QApplication([])
    adapter = MagicMock()
    adapter.get_channels.return_value = []
    view = LinksView(adapter)
    view.total = 10

    view._task_changed(
        {
            "status": "pending",
            "progress": 20,
            "not_before": to_db_time(utc_now() + timedelta(seconds=65)),
            "error": "flood_wait_deferred: Telegram FloodWait",
        }
    )

    text = view.status.text()
    assert "Telegram ограничил запросы" in text
    assert "продолжение через" in text
    assert "обработано: 2 из 10" in text
    view.deleteLater()
    app.processEvents()
