from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.config import TelegramSettings
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TelegramOperationError,
)
from services.import_service import ImportService, ImportValidationError
from core.composition import ApplicationContainer
from workers.handler_registry import create_worker_handlers


class _Worker:
    def __init__(self, database) -> None:
        self.database = database
        self.sleep_calls: list[float] = []

    def get_db(self):
        return self.database

    def isInterruptionRequested(self) -> bool:
        return False

    async def safe_sleep(self, seconds: float, *, cancel_scope=None) -> bool:
        self.sleep_calls.append(seconds)
        return True


class _Telegram:
    def __init__(self) -> None:
        self.channels: list[dict] = []
        self.saved_dialogs: list[dict] = []
        self.member = True
        self.member_results: list[bool] = []
        self.member_calls: list[object] = []
        self.join_error: Exception | None = None
        self.latest_error: Exception | None = None
        self.join_result = True
        self.join_saved_result = True
        self.join_calls: list[object] = []
        self.join_saved_calls: list[dict] = []
        self.chat_titles: dict[object, str] = {}
        self.peer_links: dict[object, object] = {}
        self.latest_post = SimpleNamespace(status="ok", message=SimpleNamespace(id=901))
        self.latest_posts: list[object] = []
        self.latest_calls = 0
        self.exact_post: object | None = None
        self.exact_calls: list[tuple[object, int]] = []

    async def iter_channels(self):
        for row in self.channels:
            yield row

    async def iter_saved_dialogs(self):
        for row in self.saved_dialogs:
            yield row

    async def iter_dialog_snapshot(self):
        for row in self.channels:
            yield {"work_target": row, "saved_dialog": None}
        for row in self.saved_dialogs:
            yield {"work_target": None, "saved_dialog": row}

    async def disconnect(self) -> None:
        return None

    async def is_member(self, chat_id) -> bool:
        self.member_calls.append(chat_id)
        if self.member_results:
            return self.member_results.pop(0)
        return self.member

    async def join(self, chat_id) -> bool:
        self.join_calls.append(chat_id)
        if self.join_error:
            raise self.join_error
        return self.join_result

    async def join_without_confirmation(self, chat_id) -> bool:
        return await self.join(chat_id)

    async def join_saved_dialog(self, **kwargs) -> bool:
        self.join_saved_calls.append(kwargs)
        if self.join_error:
            raise self.join_error
        return self.join_saved_result

    async def get_latest_post_for_commenting(self, _channel_id):
        self.latest_calls += 1
        if self.latest_error:
            raise self.latest_error
        if self.latest_posts:
            return self.latest_posts.pop(0)
        return self.latest_post

    async def get_post_for_commenting(self, channel_id, post_id):
        self.exact_calls.append((channel_id, int(post_id)))
        if self.latest_error:
            raise self.latest_error
        if self.exact_post is not None:
            return self.exact_post
        return SimpleNamespace(
            status="ok",
            message=SimpleNamespace(id=int(post_id)),
            discussion_chat_id=20,
            discussion_message_id=40,
        )

    async def get_chat_title(self, chat_id):
        return self.chat_titles.get(chat_id, f"Chat {chat_id}")

    @staticmethod
    def is_channel_peer(value):
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return False
        return numeric > 0 or str(numeric).startswith("-100")

    async def get_linked_chat(self, chat_id):
        return self.peer_links.get(chat_id)


class _Comments:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.error: Exception | None = None
        self.errors: list[Exception | None] = []

    async def ensure_and_send_comment(self, **kwargs) -> None:
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        elif self.error:
            raise self.error
        self.sent.append(kwargs)

    async def send_direct_message(self, chat_id, text, *, task_id=None, **kwargs) -> None:
        if self.error:
            raise self.error
        self.sent.append(
            {"chat_id": chat_id, "text": text, "task_id": task_id, **kwargs}
        )


class _Linked:
    def __init__(self) -> None:
        self.links: dict[object, object] = {}
        self.access: dict[object, bool] = {}
        self.error: Exception | None = None

    async def get_linked_chat_id(self, channel_id):
        if self.error:
            raise self.error
        return self.links.get(channel_id)

    async def check_access(self, linked_chat_id):
        return self.access.get(linked_chat_id, False)


def _handlers(
    monkeypatch,
    database: MagicMock,
    telegram: _Telegram,
    *,
    linked: _Linked | None = None,
    importer=None,
    link_check_delay_min: float = 0,
    link_check_delay_max: float = 0,
):
    container = object.__new__(ApplicationContainer)
    container.config = SimpleNamespace(
        rate_limit=0.01,
        max_joins_per_hour=40,
        min_join_interval_seconds=45,
        post_join_delay_min_seconds=1,
        post_join_delay_max_seconds=1,
        link_join_delay_min_seconds=15,
        link_join_delay_max_seconds=25,
        link_check_delay_min_seconds=link_check_delay_min,
        link_check_delay_max_seconds=link_check_delay_max,
    )
    container.queue_worker = _Worker(database)
    container.secret_store = MagicMock()
    container._telegram_settings = lambda _db: TelegramSettings(
        api_id=1,
        api_hash="hash",
        session_dir=Path("/tmp"),
    )

    comments = _Comments()
    linked = linked or _Linked()
    import_factory = (
        (lambda _db: importer) if importer is not None else ImportService
    )
    handlers, cleanup = create_worker_handlers(
        container,
        TelegramService=lambda *_args, **_kwargs: telegram,
        ImportService=import_factory,
        LinkedChatService=lambda _telegram: linked,
        CommentService=lambda *_args, **_kwargs: comments,
    )
    return handlers, cleanup, comments, container.queue_worker


@pytest.mark.asyncio
async def test_sync_channels_streams_in_bounded_batches(monkeypatch):
    db = MagicMock()
    batch_sizes: list[int] = []
    db.upsert_channels_batch.side_effect = lambda rows: batch_sizes.append(len(rows))
    telegram = _Telegram()
    telegram.channels = [
        {"id": index, "title": f"Channel {index}", "username": f"c{index}"}
        for index in range(450)
    ]
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["sync_channels"]({"id": 11, "payload": {}})

    assert batch_sizes == [200, 200, 50]
    assert len(db.prune_channels_except.call_args.args[0]) == 450
    db.update_task_progress.assert_any_call(11, 100)


@pytest.mark.asyncio
async def test_sync_saved_dialogs_streams_in_bounded_batches(monkeypatch):
    db = MagicMock()
    db.get_setting.side_effect = lambda key, default=None: {
        "telegram.account_id": 77,
        "telegram.phone": "+100",
    }.get(key, default)
    batch_sizes: list[int] = []
    batch_kwargs: list[dict] = []

    def record_batch(rows, **kwargs):
        batch_sizes.append(len(rows))
        batch_kwargs.append(kwargs)

    db.upsert_saved_dialogs_batch.side_effect = record_batch
    telegram = _Telegram()
    telegram.saved_dialogs = [
        {"peer_id": index, "title": f"Dialog {index}", "kind": "channel"}
        for index in range(401)
    ]
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["sync_saved_dialogs"]({"id": 12, "payload": {}})

    assert batch_sizes == [200, 200, 1]
    assert batch_kwargs == [
        {"account_id": 77, "phone": "+100"},
        {"account_id": 77, "phone": "+100"},
        {"account_id": 77, "phone": "+100"},
    ]
    db.update_task_progress.assert_any_call(12, 100)


@pytest.mark.asyncio
async def test_comment_slot_trusts_links_preflight_without_membership_rpc(
    monkeypatch,
):
    db = MagicMock()
    db.get_setting.return_value = 77
    db.get_comment_campaign.return_value = {
        "id": 1,
        "status": "running",
        "comments": ["hello"],
        "last_comment_text": "",
    }
    db.mark_comment_slot_running.return_value = True
    db.get_channels_for_commenting.return_value = [
        {"channel_id": 10, "linked_chat_id": 20, "title": "Channel"}
    ]
    db.has_commented.return_value = False
    telegram = _Telegram()
    telegram.member_results = [False]
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 13, "payload": {"campaign_id": 1, "slot_id": 2}}
    )

    db.get_join_guard.assert_not_called()
    assert telegram.join_calls == []
    db.record_join_event.assert_not_called()
    assert telegram.member_calls == []
    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_join_slot_long_flood_wait_is_deferred_without_finishing(monkeypatch):
    db = MagicMock()
    context = {
        "campaign_status": "running",
        "status": "queued",
        "title": "Saved",
        "account_id": 5,
        "saved_dialog_id": 6,
        "peer_id": 7,
        "username": "saved",
        "invite_link": None,
        "max_per_hour": 40,
    }
    db.get_join_slot_context.side_effect = [context, {**context, "status": "pending"}]
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    telegram = _Telegram()
    telegram.member = False
    telegram.join_error = DeferredTelegramError(
        "wait", code="flood_wait_deferred", retry_after=90
    )
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["join_saved_slot"](
        {"id": 14, "payload": {"campaign_id": 1, "slot_id": 2}}
    )

    db.defer_join_slot_and_set_network_wait.assert_called_once()
    db.defer_join_slot.assert_not_called()
    db.set_join_campaign_network_wait.assert_not_called()
    db.finish_join_slot.assert_not_called()


@pytest.mark.asyncio
async def test_uncertain_join_bans_target_and_keeps_campaign_running(
    monkeypatch,
):
    db = MagicMock()
    context = {
        "campaign_status": "running",
        "status": "queued",
        "title": "Saved",
        "account_id": 5,
        "saved_dialog_id": 6,
        "peer_id": 7,
        "username": "saved",
        "invite_link": None,
        "max_per_hour": 40,
    }
    db.get_join_slot_context.side_effect = [context, {**context, "status": "running"}]
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    telegram = _Telegram()
    telegram.member = False
    from core.exceptions import NonRetryableTelegramError

    telegram.join_error = NonRetryableTelegramError(
        "unknown", code="join_result_unknown"
    )
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["join_saved_slot"](
        {"id": 15, "payload": {"campaign_id": 1, "slot_id": 2}}
    )

    db.set_saved_dialog_membership.assert_called_once_with(6, 5, "uncertain", "unknown")
    db.ban_peer_locally.assert_called_once_with(
        7,
        "Результат вступления неизвестен; цель локально заблокирована",
        account_id=5,
    )
    db.pause_join_campaign.assert_not_called()
    db.finish_join_slot.assert_called_once()
    assert db.finish_join_slot.call_args.kwargs["status"] == "uncertain"


def _join_context(**overrides):
    context = {
        "campaign_status": "running",
        "status": "queued",
        "title": "Saved",
        "account_id": 5,
        "saved_dialog_id": 6,
        "peer_id": 7,
        "username": "saved",
        "invite_link": None,
        "max_per_hour": 40,
    }
    context.update(overrides)
    return context


def _comment_database(**campaign_overrides):
    db = MagicMock()
    campaign = {
        "id": 1,
        "status": "running",
        "comments": ["first", "second"],
        "last_comment_text": "first",
        "network_failure_count": 0,
    }
    campaign.update(campaign_overrides)
    db.get_comment_campaign.return_value = campaign
    db.mark_comment_slot_running.return_value = True
    db.get_channels_for_commenting.return_value = [
        {
            "channel_id": 10,
            "linked_chat_id": 20,
            "title": "Channel",
            "username": "channel",
        }
    ]
    db.has_commented.return_value = False
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    return db


def test_settings_helpers_and_saved_telegram_settings(tmp_path):
    container = object.__new__(ApplicationContainer)
    container.config = SimpleNamespace(
        telegram=TelegramSettings(
            api_id=100,
            api_hash="fallback",
            session_dir=tmp_path,
            phone="+000",
        )
    )
    container.secret_store = MagicMock()
    container.secret_store.get.side_effect = lambda key, default="": {
        "telegram.api_hash": "secret-hash",
        "telegram.phone": " +123 ",
        "telegram.proxy_username": " user ",
        "telegram.proxy_password": "secret-password",
    }.get(key, default)
    db = MagicMock()
    db.get_settings.return_value = {
        "telegram.api_id": " 777 ",
        "telegram.proxy_enabled": "YES",
        "telegram.proxy_type": "http",
        "telegram.proxy_host": " proxy.local ",
        "telegram.proxy_port": "1080",
    }

    settings = container._telegram_settings(db)

    assert ApplicationContainer._as_bool(" On ") is True
    assert ApplicationContainer._as_bool("no") is False
    assert ApplicationContainer._as_int("bad", 9) == 9
    assert settings.api_id == 777
    assert settings.api_hash == "secret-hash"
    assert settings.phone == "+123"
    assert settings.proxy_enabled is True
    assert settings.proxy_type == "HTTP"
    assert settings.proxy_port == 1080
    assert settings.proxy_username == "user"
    assert settings.proxy_password == "secret-password"


@pytest.mark.asyncio
async def test_unconfigured_telegram_handlers_fail_closed(monkeypatch):
    db = MagicMock()
    container = object.__new__(ApplicationContainer)
    container.config = SimpleNamespace(rate_limit=0.01)
    container.queue_worker = _Worker(db)
    container.secret_store = MagicMock()
    container._telegram_settings = lambda _db: TelegramSettings(
        api_id=0, api_hash="", session_dir=Path("/tmp")
    )

    # This test verifies the single-account factory's fail-closed behavior.
    # The production container now wraps that factory in a multi-account runtime
    # manager, whose cleanup is intentionally an async close callback.
    handlers, cleanup = create_worker_handlers(
        container,
        TelegramService=MagicMock,
        ImportService=ImportService,
        LinkedChatService=MagicMock,
        CommentService=MagicMock,
    )

    assert cleanup is None
    with pytest.raises(NonRetryableTelegramError) as raised:
        await handlers["sync_channels"]({"id": 1, "payload": {}})
    assert raised.value.code == "telegram_not_configured"
    await handlers["noop"]({"id": 2})


@pytest.mark.asyncio
async def test_import_handler_validates_and_maps_legacy_payload(monkeypatch):
    db = MagicMock()
    telegram = _Telegram()
    importer = MagicMock()
    importer.migrate.return_value = {"errors": []}
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, telegram, importer=importer
    )

    with pytest.raises(NonRetryableTelegramError) as invalid:
        await handlers["import"]({"id": 1, "payload": {"files": "not-a-map"}})
    assert invalid.value.code == "invalid_payload"

    await handlers["import"](
        {"id": 2, "payload": {"kind": "channels", "path": "/tmp/channels.csv"}}
    )
    importer.migrate.assert_called_once_with({"channels": "/tmp/channels.csv"})


@pytest.mark.asyncio
async def test_import_handler_converts_validation_and_report_errors(monkeypatch):
    db = MagicMock()
    telegram = _Telegram()
    importer = MagicMock()
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, telegram, importer=importer
    )

    importer.migrate.side_effect = ImportValidationError("broken csv")
    with pytest.raises(NonRetryableTelegramError) as validation:
        await handlers["import"]({"id": 1, "payload": {"files": {"channels": "x"}}})
    assert validation.value.code == "invalid_payload"

    importer.migrate.side_effect = None
    importer.migrate.return_value = {
        "errors": [{"kind": "channels", "error": "bad row"}]
    }
    with pytest.raises(NonRetryableTelegramError) as report_error:
        await handlers["import"]({"id": 2, "payload": {"files": {"channels": "x"}}})
    assert report_error.value.code == "import_failed"
    assert "channels: bad row" in str(report_error.value)


@pytest.mark.asyncio
async def test_link_channels_persists_missing_and_access_states(monkeypatch):
    db = MagicMock()
    db.get_channels.return_value = [
        {"channel_id": 1, "title": "No discussion"},
        {"channel_id": 2, "title": "Member"},
        {"channel_id": 3, "title": "Needs join"},
    ]
    linked = _Linked()
    linked.links = {1: None, 2: 20, 3: 30}
    linked.access = {20: True, 30: False}
    telegram = _Telegram()
    telegram.chat_titles = {20: "Discussion 20", 30: "Discussion 30"}
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, telegram, linked=linked
    )

    await handlers["link_channels"]({"id": 9, "payload": {}})

    assert db.update_channel_link.call_args_list[0].args == (
        1,
        None,
        None,
        "Нет чата обсуждения",
    )
    assert db.update_channel_link.call_args_list[1].args == (
        2,
        20,
        None,
        "Связано · вступление выполнено",
    )
    assert db.update_channel_link.call_args_list[2].args == (
        3,
        30,
        None,
        "Связано · вступление выполнено",
    )
    assert telegram.join_calls == [20, 30]
    assert db.record_join_event.call_count == 2
    db.update_task_progress.assert_called_with(9, 100)


@pytest.mark.asyncio
async def test_link_channels_records_nonfatal_access_error(monkeypatch):
    db = MagicMock()
    db.get_channels.return_value = [
        {
            "channel_id": 1,
            "title": "Private",
            "linked_chat_id": 99,
            "linked_chat_title": "Old",
        }
    ]
    linked = _Linked()
    linked.error = NonRetryableTelegramError("private", code="channel_private")
    handlers, _cleanup, _comments, _worker = _handlers(
        monkeypatch, db, _Telegram(), linked=linked
    )

    await handlers["link_channels"]({"id": 9, "payload": {}})

    db.update_channel_link.assert_called_once_with(1, 99, None, "Недоступно: private")


@pytest.mark.asyncio
async def test_join_slot_guard_and_existing_membership_paths(monkeypatch):
    db = MagicMock()
    context = _join_context()
    db.get_join_slot_context.side_effect = [context, {**context, "status": "pending"}]
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": False, "wait_seconds": 60}
    telegram = _Telegram()
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["join_saved_slot"](
        {"id": 20, "payload": {"campaign_id": 1, "slot_id": 2}}
    )
    db.defer_join_slot.assert_called_once()
    db.finish_join_slot.assert_not_called()

    db.reset_mock()
    context = _join_context()
    db.get_join_slot_context.side_effect = [context, {**context, "status": "running"}]
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    telegram.member = True
    telegram.join_saved_result = False

    await handlers["join_saved_slot"](
        {"id": 21, "payload": {"campaign_id": 1, "slot_id": 3}}
    )
    db.set_saved_dialog_membership.assert_called_with(6, 5, "member")
    assert db.finish_join_slot.call_args.kwargs["status"] == "already_member"


@pytest.mark.asyncio
async def test_join_slot_success_and_already_result_are_accounted(monkeypatch):
    db = MagicMock()
    context = _join_context()
    db.get_join_slot_context.side_effect = [context, {**context, "status": "running"}]
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    telegram = _Telegram()
    telegram.member = False
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["join_saved_slot"](
        {"id": 22, "payload": {"campaign_id": 1, "slot_id": 4}}
    )
    db.record_join_event.assert_called_once()
    assert db.finish_join_slot.call_args.kwargs == {
        "status": "joined",
        "result": "Вступление выполнено",
        "joined": True,
    }

    db.reset_mock()
    db.get_join_slot_context.side_effect = [context, {**context, "status": "running"}]
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    telegram.join_saved_result = False

    await handlers["join_saved_slot"](
        {"id": 23, "payload": {"campaign_id": 1, "slot_id": 5}}
    )
    db.record_join_event.assert_not_called()
    assert db.finish_join_slot.call_args.kwargs["status"] == "already_member"


@pytest.mark.asyncio
async def test_join_slot_network_and_safety_failures(monkeypatch):
    db = MagicMock()
    context = _join_context()
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    telegram = _Telegram()
    telegram.member = False
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    db.get_join_slot_context.side_effect = [context, {**context, "status": "pending"}]
    telegram.join_error = NonRetryableTelegramError(
        "offline", code="network_unavailable"
    )
    await handlers["join_saved_slot"](
        {"id": 24, "payload": {"campaign_id": 1, "slot_id": 6}}
    )
    db.defer_join_slot_and_set_network_wait.assert_called_once()
    db.defer_join_slot.assert_not_called()
    db.set_join_campaign_network_wait.assert_not_called()
    db.finish_join_slot.assert_not_called()

    db.reset_mock()
    db.mark_join_slot_running.return_value = True
    db.get_join_guard.return_value = {"allowed": True, "wait_seconds": 0}
    db.get_join_slot_context.side_effect = [context, {**context, "status": "running"}]
    telegram.join_error = NonRetryableTelegramError("limited", code="peer_flood")
    await handlers["join_saved_slot"](
        {"id": 25, "payload": {"campaign_id": 1, "slot_id": 7}}
    )
    db.pause_join_campaign.assert_called_once()
    db.set_saved_dialog_membership.assert_called_with(6, 5, "failed", "limited")
    assert db.finish_join_slot.call_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_direct_message_comment_and_legacy_handlers(monkeypatch):
    db = MagicMock()
    telegram = _Telegram()
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    with pytest.raises(NonRetryableTelegramError) as disabled_empty:
        await handlers["direct_message"]({"id": 1, "payload": {"chat_id": 10}})
    assert disabled_empty.value.code == "direct_group_disabled"
    with pytest.raises(NonRetryableTelegramError) as disabled_text:
        await handlers["direct_message"](
            {"id": 2, "payload": {"chat_id": 10, "text": " hello "}}
        )
    assert disabled_text.value.code == "direct_group_disabled"
    assert comments.sent == []

    with pytest.raises(NonRetryableTelegramError):
        await handlers["comment"]({"id": 3, "payload": {"post_id": 1}})
    await handlers["comment"](
        {
            "id": 4,
            "payload": {
                "channel_id": 10,
                "linked_chat_id": 20,
                "post_id": 30,
                "reply_to": 40,
                "text": "comment",
            },
        }
    )
    assert comments.sent[-1]["post_message_id"] == 30
    assert comments.sent[-1]["linked_chat_id"] == 20
    assert comments.sent[-1]["reply_to"] == 40
    assert telegram.exact_calls == [(10, 30)]
    assert telegram.latest_calls == 0

    with pytest.raises(NonRetryableTelegramError) as legacy:
        await handlers["auto_comment"]({"id": 5, "payload": {}})
    assert legacy.value.code == "legacy_batch_disabled"


@pytest.mark.asyncio
async def test_comment_slot_preconditions(monkeypatch):
    telegram = _Telegram()

    db = _comment_database()
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)
    with pytest.raises(NonRetryableTelegramError) as invalid:
        await handlers["auto_comment_slot"]({"id": 1, "payload": {}})
    assert invalid.value.code == "invalid_payload"

    db.get_comment_campaign.return_value = None
    with pytest.raises(NonRetryableTelegramError) as missing:
        await handlers["auto_comment_slot"](
            {"id": 2, "payload": {"campaign_id": 1, "slot_id": 2}}
        )
    assert missing.value.code == "campaign_missing"

    db.get_comment_campaign.return_value = {"status": "paused", "comments": ["x"]}
    await handlers["auto_comment_slot"](
        {"id": 3, "payload": {"campaign_id": 1, "slot_id": 3}}
    )
    db.defer_comment_slot.assert_called_once()
    db.finish_comment_slot.assert_not_called()

    db.get_comment_campaign.return_value = {"status": "running", "comments": [" "]}
    await handlers["auto_comment_slot"](
        {"id": 4, "payload": {"campaign_id": 1, "slot_id": 4}}
    )
    db.pause_campaign_for_safety.assert_called()
    assert db.finish_comment_slot.call_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_comment_slot_unavailable_or_without_channels(monkeypatch):
    telegram = _Telegram()
    db = _comment_database()
    db.mark_comment_slot_running.return_value = False
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    with pytest.raises(NonRetryableTelegramError) as unavailable:
        await handlers["auto_comment_slot"](
            {"id": 5, "payload": {"campaign_id": 1, "slot_id": 5}}
        )
    assert unavailable.value.code == "campaign_slot_unavailable"

    db.mark_comment_slot_running.return_value = True
    db.get_channels_for_commenting.return_value = []
    await handlers["auto_comment_slot"](
        {"id": 6, "payload": {"campaign_id": 1, "slot_id": 6}}
    )
    db.add_comment_history.assert_called()
    db.finish_comment_slot.assert_called_with(
        6,
        status="skipped",
        result=(
            "Кампания завершена: все доступные каналы и группы уже "
            "обработаны за последние 24 часа"
        ),
    )
    db.complete_comment_campaign.assert_called_once()


@pytest.mark.asyncio
async def test_continuous_comment_slot_without_channels_waits_for_next_cycle(
    monkeypatch,
):
    telegram = _Telegram()
    db = _comment_database(continuous=True)
    db.get_channels_for_commenting.return_value = []
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 66, "payload": {"campaign_id": 1, "slot_id": 66}}
    )

    db.finish_comment_slot.assert_called_with(
        66,
        status="skipped",
        result=(
            "Все доступные каналы и группы уже обработаны за последние "
            "24 часа; ожидание следующего цикла"
        ),
    )
    db.complete_comment_campaign.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("no_post", "нет публикаций"),
        ("comments_disabled", "комментарии отключены"),
        ("discussion_missing", "ветка последнего поста"),
        ("other", "последний пост недоступен"),
    ],
)
async def test_comment_slot_skips_unavailable_posts(monkeypatch, status, expected):
    db = _comment_database()
    telegram = _Telegram()
    telegram.latest_post = SimpleNamespace(status=status, message=None)
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 7, "payload": {"campaign_id": 1, "slot_id": 7}}
    )

    assert expected in db.finish_comment_slot.call_args.kwargs["result"]
    assert comments.sent == []


@pytest.mark.asyncio
async def test_comment_slot_skips_previously_commented_post(monkeypatch):
    db = _comment_database()
    db.has_commented.return_value = True
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, _Telegram())

    await handlers["auto_comment_slot"](
        {"id": 8, "payload": {"campaign_id": 1, "slot_id": 8}}
    )

    assert "уже комментировали" in db.finish_comment_slot.call_args.kwargs["result"]
    assert comments.sent == []


@pytest.mark.asyncio
async def test_comment_slot_sends_as_member_and_rotates_text(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member = True
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 9, "payload": {"campaign_id": 1, "slot_id": 9}}
    )

    assert comments.sent == [
        {
            "linked_chat_id": 20,
            "post_message_id": 901,
            "text": "second",
            "channel_id": 10,
            "membership_ready": True,
            "account_id": 0,
            "campaign_id": 1,
            "action_type": "campaign_comment",
        }
    ]
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"
    assert db.finish_comment_slot.call_args.kwargs["sent"] is True


@pytest.mark.asyncio
async def test_comment_slot_does_not_repeat_membership_check_before_send(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [False]
    handlers, _cleanup, comments, worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 10, "payload": {"campaign_id": 1, "slot_id": 10}}
    )

    assert telegram.join_calls == []
    db.record_join_event.assert_not_called()
    assert worker.sleep_calls == []
    assert telegram.member_calls == []
    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_comment_slot_sends_for_prepared_member_without_join(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.member_results = [True]
    handlers, _cleanup, comments, worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 11, "payload": {"campaign_id": 1, "slot_id": 11}}
    )

    assert telegram.join_calls == []
    db.record_join_event.assert_not_called()
    assert worker.sleep_calls == []
    assert len(comments.sent) == 1
    assert db.finish_comment_slot.call_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_comment_slot_deferred_and_network_errors_do_not_finish(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.latest_error = DeferredTelegramError(
        "wait", code="flood_wait_deferred", retry_after=90
    )
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)

    await handlers["auto_comment_slot"](
        {"id": 12, "payload": {"campaign_id": 1, "slot_id": 12}}
    )
    db.defer_comment_slot_and_set_network_wait.assert_called_once()
    db.defer_comment_slot.assert_not_called()
    db.set_campaign_network_wait.assert_not_called()
    db.finish_comment_slot.assert_not_called()

    db = _comment_database(network_failure_count=2)
    telegram = _Telegram()
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)
    comments.error = NonRetryableTelegramError("offline", code="network_unavailable")
    await handlers["auto_comment_slot"](
        {"id": 13, "payload": {"campaign_id": 1, "slot_id": 13}}
    )
    db.defer_comment_slot_and_set_network_wait.assert_called_once()
    db.defer_comment_slot.assert_not_called()
    db.set_campaign_network_wait.assert_not_called()
    db.finish_comment_slot.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "paused", "deferred"),
    [
        (
            NonRetryableTelegramError("unknown", code="delivery_result_unknown"),
            "uncertain",
            True,
            False,
        ),
        (
            NonRetryableTelegramError("forbidden", code="chat_write_forbidden"),
            "skipped",
            False,
            False,
        ),
        (TelegramOperationError("temporary"), None, False, True),
    ],
)
async def test_comment_slot_classifies_failures(
    monkeypatch, error, status, paused, deferred
):
    db = _comment_database()
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, _Telegram())
    comments.error = error

    await handlers["auto_comment_slot"](
        {"id": 14, "payload": {"campaign_id": 1, "slot_id": 14}}
    )

    if deferred:
        db.defer_comment_slot_and_set_network_wait.assert_called_once()
        db.defer_comment_slot.assert_not_called()
        db.set_campaign_network_wait.assert_not_called()
        db.finish_comment_slot.assert_not_called()
    else:
        assert db.finish_comment_slot.call_args.kwargs["status"] == status
    assert db.pause_campaign_for_safety.called is paused


@pytest.mark.asyncio
async def test_internal_pre_send_error_pauses_campaign_and_is_not_swallowed(
    monkeypatch,
):
    db = _comment_database()
    telegram = _Telegram()
    telegram.latest_error = RuntimeError("broken target resolver")
    handlers, _cleanup, _comments, _worker = _handlers(monkeypatch, db, telegram)

    with pytest.raises(RuntimeError, match="broken target resolver"):
        await handlers["auto_comment_slot"](
            {"id": 15, "payload": {"campaign_id": 1, "slot_id": 15}}
        )

    db.pause_campaign_for_safety.assert_called_once()
    assert db.finish_comment_slot.call_args.kwargs["status"] == "failed"
    db.mark_channel_comment_checked.assert_not_called()


@pytest.mark.asyncio
async def test_internal_post_dispatch_error_is_uncertain_and_pauses(monkeypatch):
    db = _comment_database()
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, _Telegram())
    comments.error = RuntimeError("send wrapper crashed")

    with pytest.raises(RuntimeError, match="send wrapper crashed"):
        await handlers["auto_comment_slot"](
            {"id": 16, "payload": {"campaign_id": 1, "slot_id": 16}}
        )

    db.pause_campaign_for_safety.assert_called_once()
    assert db.finish_comment_slot.call_args.kwargs["status"] == "uncertain"
    db.mark_channel_comment_checked.assert_called_once()


@pytest.mark.asyncio
async def test_comment_failure_log_contains_target_ids_and_rpc_error(monkeypatch):
    db = _comment_database()
    telegram = _Telegram()
    telegram.latest_post = SimpleNamespace(
        status="ok",
        message=SimpleNamespace(id=901),
        discussion_chat_id=20,
        discussion_message_id=777,
    )
    handlers, _cleanup, comments, _worker = _handlers(monkeypatch, db, telegram)
    comments.error = NonRetryableTelegramError(
        "forbidden",
        code="chat_write_forbidden",
        details={
            "rpc_error": "ChatWriteForbiddenError",
            "rpc_message": "You cannot write in this chat",
        },
    )

    await handlers["auto_comment_slot"](
        {"id": 99, "payload": {"campaign_id": 1, "slot_id": 99}}
    )

    result = db.finish_comment_slot.call_args.kwargs["result"]
    assert "channel_id=10" in result
    assert "post_id=901" in result
    assert "linked_chat_id=20" in result
    assert "discussion_message_id=777" in result
    assert "rpc=ChatWriteForbiddenError" in result
    db.insert_log.assert_called()
