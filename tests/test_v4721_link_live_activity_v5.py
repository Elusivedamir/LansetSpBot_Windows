from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from core.campaign_schedule import to_db_time, utc_now
from core.exceptions import DeferredTelegramError
from gui.activity_panel import ActivityPanel
from storage.database import Database
from tests.test_composition_resilience import _Linked, _Telegram, _handlers
from workers.queue_worker import QueueWorker


@pytest.mark.asyncio
async def test_link_handler_mirrors_major_steps_to_persistent_activity(monkeypatch):
    database = MagicMock()
    database.get_setting.return_value = 77
    database.get_channels.return_value = [
        {"channel_id": 1, "title": "One", "target_kind": "channel"},
        {"channel_id": 2, "title": "Two", "target_kind": "channel"},
    ]
    linked = _Linked()
    linked.links = {1: None, 2: None}
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch,
        database,
        _Telegram(),
        linked=linked,
        link_check_delay_min=3,
        link_check_delay_max=3,
    )

    await handlers["link_channels"]({"id": 71, "payload": {"account_id": 77}})

    messages = [str(call.args[1]) for call in database.insert_log.call_args_list]
    assert any(
        "Проверка новых, изменившихся и устаревших связок запущена" in message
        for message in messages
    )
    assert any("Пауза между каналами:" in message for message in messages)
    assert any("Связка 1 из 2: One" in message for message in messages)
    assert any("One · нет чата обсуждения" in message for message in messages)
    assert any("Связки подготовлены" in message for message in messages)


@pytest.mark.asyncio
async def test_link_floodwait_is_written_to_live_journal_database(tmp_path):
    database = Database(tmp_path / "link-live-floodwait.db")
    database.set_setting("telegram.account_id", 77)
    task_id = database.insert_task("link_channels", {"account_id": 77})
    task = database.claim_next_pending_task()
    assert task is not None
    database.update_task_checkpoint(task_id, {"account_id": 77}, 35)

    async def defer(_task):
        raise DeferredTelegramError(
            "Telegram FloodWait", code="flood_wait_deferred", retry_after=1272
        )

    worker = QueueWorker(lambda: {})
    worker._db = database
    worker._handlers = {"link_channels": defer}
    await worker._process_task(task)

    rows = database.get_logs(limit=20)
    messages = [str(row.get("message") or "") for row in rows]
    assert any(
        "[Связки] Telegram установил FloodWait" in message for message in messages
    )
    assert any("35%" in message for message in messages)
    assert any("21 мин 12 сек" in message for message in messages)
    assert any(
        "Канал, вызвавший FloodWait, пропущен" in message for message in messages
    )
    assert any("продолжится со следующего объекта" in message for message in messages)


def test_activity_panel_shows_link_countdown_and_resume_in_shared_feed():
    app = QApplication.instance() or QApplication([])

    class Adapter:
        def __init__(self):
            self.logs = [
                {
                    "id": 1,
                    "level": "WARNING",
                    "message": (
                        "[Связки] Telegram установил FloodWait. Связки сохранены "
                        "на 20%. Канал, вызвавший FloodWait, пропущен; "
                        "работа продолжится со следующего объекта через 1 мин 5 сек."
                    ),
                }
            ]
            self.task = {
                "id": 91,
                "type": "link_channels",
                "status": "pending",
                "progress": 20,
                "not_before": to_db_time(utc_now() + timedelta(seconds=65)),
                "error": "flood_wait_deferred: Telegram FloodWait",
            }

        def get_logs(self, level=None, limit=100):
            rows = self.logs
            if level:
                rows = [row for row in rows if row["level"] == level]
            return list(reversed(rows[-limit:]))

        def get_tasks(self, status=None, limit=100):
            return [dict(self.task)]

        def get_scheduler_error(self):
            return ""

        def get_comment_campaign_state(self):
            return None

        def get_join_campaign_state(self):
            return None

    adapter = Adapter()
    panel = ActivityPanel(adapter)
    panel.timer.stop()
    panel.refresh()
    assert "Связки · FloodWait · 20%" in panel.state_label.text()
    assert "Продолжение через" in panel.next_label.text()
    assert "Telegram установил FloodWait" in panel.feed.toPlainText()

    adapter.task["not_before"] = to_db_time(utc_now() + timedelta(seconds=55))
    panel.refresh()
    assert "FloodWait продолжается" in panel.feed.toPlainText()

    adapter.task.update(
        {
            "status": "running",
            "not_before": None,
            "status_text": "Связка 3 из 10: Test",
        }
    )
    panel.refresh()
    text = panel.feed.toPlainText()
    assert "FloodWait завершён" in text
    assert "без повторного обхода" in text
    assert "Связки · выполняются · 20%" in panel.state_label.text()

    panel.deleteLater()
    app.processEvents()
