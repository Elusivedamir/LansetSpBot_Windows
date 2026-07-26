from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gui.views.channels_view import ChannelsView
from tests.test_composition_resilience import _Telegram, _handlers


@pytest.mark.asyncio
async def test_sync_channels_writes_start_progress_and_finish_to_live_journal(monkeypatch):
    db = MagicMock()
    db.get_setting.side_effect = lambda key, default=None: {
        "telegram.account_id": 77,
        "telegram.phone": "+100",
    }.get(key, default)
    db.upsert_saved_dialogs_batch.side_effect = lambda rows, **_kwargs: [
        row["peer_id"] for row in rows
    ]

    telegram = _Telegram()
    for index in range(11):
        telegram.channels.append(
            {
                "id": index + 1,
                "title": f"Channel {index + 1}",
                "username": f"channel_{index + 1}",
                "target_kind": "channel",
                "comment_mode": "channel_post",
            }
        )
        telegram.saved_dialogs.append(
            {
                "peer_id": -(10_000 + index),
                "title": f"Channel {index + 1}",
                "username": f"channel_{index + 1}",
                "kind": "channel",
                "invite_link": None,
            }
        )

    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, telegram
    )
    await handlers["sync_channels"]({"id": 42, "payload": {}})

    rendered = [call.args[1] for call in db.insert_log.call_args_list]
    assert rendered[0] == "[Каналы] Начато получение списка каналов и групп"
    assert any(
        line.startswith("[Каналы] Обработано каналов и групп: 10")
        for line in rendered
    )
    assert rendered[-1] == (
        "[Каналы] Список обновлён · найдено каналов и групп: 11 · "
        "рабочих целей: 11"
    )


@pytest.mark.asyncio
async def test_sync_channels_writes_error_to_live_journal(monkeypatch):
    db = MagicMock()
    db.get_setting.side_effect = lambda key, default=None: {
        "telegram.account_id": 77,
        "telegram.phone": "+100",
    }.get(key, default)

    class BrokenTelegram(_Telegram):
        async def iter_dialog_snapshot(self):
            raise RuntimeError("network failed")
            yield  # pragma: no cover

    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, BrokenTelegram()
    )

    with pytest.raises(RuntimeError, match="network failed"):
        await handlers["sync_channels"]({"id": 43, "payload": {}})

    levels_and_lines = [
        (call.args[0], call.args[1]) for call in db.insert_log.call_args_list
    ]
    assert levels_and_lines[-1] == (
        "ERROR",
        "[Каналы] Ошибка получения списка после 0 элементов: "
        "RuntimeError: network failed",
    )


def test_join_pause_and_stop_are_disabled_when_campaign_is_not_running():
    view = SimpleNamespace(
        join_summary=MagicMock(),
        pause_join_button=MagicMock(),
        stop_join_button=MagicMock(),
    )

    ChannelsView._apply_join_state(view, None)

    view.pause_join_button.setText.assert_called_once_with("Пауза")
    view.pause_join_button.setEnabled.assert_called_once_with(False)
    view.stop_join_button.setEnabled.assert_called_once_with(False)


def test_join_pause_and_stop_are_enabled_only_for_active_campaign():
    view = SimpleNamespace(
        join_summary=MagicMock(),
        pause_join_button=MagicMock(),
        stop_join_button=MagicMock(),
    )

    ChannelsView._apply_join_state(
        view,
        {
            "status": "running",
            "attempted_count": 1,
            "total_count": 10,
            "joined_count": 1,
            "next_scheduled_display": "05:55",
        },
    )

    view.pause_join_button.setEnabled.assert_called_once_with(True)
    view.stop_join_button.setEnabled.assert_called_once_with(True)
