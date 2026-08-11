from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import types, utils

from core.exceptions import NonRetryableTelegramError
from services.account_context import AccountQueueWorkerView
from services.account_sessions import lifecycle_journal_key, recover_account_lifecycle
from services.api_parts.accounts import AccountsAPIMixin
from services.multiaccount_scheduler import AccountCampaignDatabaseView
from services.telegram.membership import TelegramMembershipMixin
from services.telegram.transport import TelegramTransportMixin
from storage.database import Database
from workers.handlers.warmup_step import (
    _PENDING_MEMBERSHIP_ERRORS,
    _REACTION_SKIP_ERRORS,
    _UNAVAILABLE_GROUP_ERRORS,
    _warmup_error_key,
)

ROOT = Path(__file__).resolve().parents[1]


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str, default=""):
        return self.values.get(str(key), default)

    def get_strict_optional(self, key: str):
        return self.values.get(str(key))

    def set(self, key: str, value) -> None:
        if value in (None, ""):
            self.values.pop(str(key), None)
        else:
            self.values[str(key)] = str(value)

    def delete(self, key: str) -> None:
        self.values.pop(str(key), None)

    def export_snapshot(self) -> dict[str, str]:
        return dict(self.values)


class _AccountAPI(AccountsAPIMixin):
    def __init__(self, db: Database, store: _MemorySecretStore, session_dir: Path) -> None:
        import threading
        self.database = db
        self.secret_store = store
        self.config = SimpleNamespace(telegram=SimpleNamespace(session_dir=session_dir))
        self.queue_worker = None
        self._secret_lock = threading.RLock()


def _register(db: Database, account_id: int, *, name: str | None = None) -> None:
    db.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=name or f"Account {account_id}",
        username=f"user_{account_id}",
        phone="+491234567890",
        authorized=True,
    )


def test_p01_authorized_flow_never_pre_saves_into_selected_account() -> None:
    source = (ROOT / "gui/views/account_parts/auth_flow.py").read_text(encoding="utf-8")
    method = source[source.index("    def _authorized("):source.index("    def _apply_authorized_account(")]
    assert ".save_settings(" not in method
    assert ".register_authorized_account(" in method


def test_p02_schedule_save_captures_explicit_owner() -> None:
    source = (ROOT / "gui/views/account_parts/settings.py").read_text(encoding="utf-8")
    method = source[source.index("    def save_activity_schedule("):source.index("    def _settings(")]
    assert "owner = int(self.adapter.get_selected_account_id()" in method
    assert "save_account_settings(" in method
    assert "account_id=owner" in method


def test_p03_sync_new_channels_recovers_to_pending(tmp_path: Path) -> None:
    db = Database(tmp_path / "tasks.db")
    _register(db, 301)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO tasks(account_id, type, payload, status, progress, max_retries,
                                 created_at, updated_at)
               VALUES(301, 'sync_new_channels', '{"account_id":301}', 'running', 0, 0,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        )
    db.reset_running_tasks()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE account_id=301 AND type='sync_new_channels'"
        ).fetchone()
    assert row is not None and row["status"] == "pending"


@pytest.mark.parametrize("operation", ["disconnect", "delete"])
def test_p04_late_final_journal_failure_never_resurrects_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    db = Database(tmp_path / f"{operation}.db")
    store = _MemorySecretStore()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    api = _AccountAPI(db, store, sessions)
    _register(db, 302)
    db.select_telegram_account(302)
    (sessions / "account_302.session").write_bytes(b"session")

    import services.api_parts.accounts as accounts_module
    real_update = accounts_module.update_account_lifecycle_journal

    def fail_final(secret_store, journal, *, phase, **updates):
        if phase == "committed":
            raise OSError("simulated late journal failure")
        return real_update(secret_store, journal, phase=phase, **updates)

    monkeypatch.setattr(accounts_module, "update_account_lifecycle_journal", fail_final)
    monkeypatch.setattr(
        "services.telegram_service.TelegramService._secure_session_file",
        staticmethod(lambda _path: None),
    )

    if operation == "disconnect":
        result = api.disconnect_telegram_account(302)
        account = db.get_telegram_account(302)
        assert account is not None and account["authorized"] in (0, False)
    else:
        result = api.delete_telegram_account(302)
        assert db.get_telegram_account(302) is None

    assert result["lifecycle_recovery_warning"]
    assert not (sessions / "account_302.session").exists()
    assert store.get(lifecycle_journal_key(302), None) is not None
    assert recover_account_lifecycle(db, store, sessions)["recovered"] == 1
    assert store.get(lifecycle_journal_key(302), None) is None
    assert not (sessions / "account_302.session").exists()


def test_p05_no_database_write_is_nested_under_secret_lock() -> None:
    accounts = (ROOT / "services/api_parts/accounts.py").read_text(encoding="utf-8")
    settings = (ROOT / "services/api_parts/settings.py").read_text(encoding="utf-8")
    openai = (ROOT / "services/api_parts/openai_comments.py").read_text(encoding="utf-8")
    persist = accounts[accounts.index("    def _persist_saved_account_settings("):accounts.index("    def save_account_settings(")]
    generic = settings[settings.index("    def save_settings("):settings.index("    def get_current_account_id(")]
    ai = openai[openai.index("    def save_openai_configuration("):openai.index("    def submit_openai_test(")]
    def initial_secret_block(method: str, marker: str) -> str:
        start = method.index(marker)
        end = method.index("\n\n        try:", start)
        return method[start:end]

    persist_secret = initial_secret_block(persist, "        with self._secret_lock:")
    generic_secret = initial_secret_block(generic, "        with self._secret_lock:")
    ai_secret = initial_secret_block(ai, "        with lock:")

    assert "writer(owner, public)" not in persist_secret
    assert "set_account_settings_with_selected_projection" not in generic_secret
    assert "database.set_settings(public)" not in ai_secret
    assert "writer(owner, public)" in persist
    assert "set_account_settings_with_selected_projection" in generic
    assert "database.set_settings(public)" in ai


def test_p06_warmup_classifies_normalized_transport_codes() -> None:
    assert _warmup_error_key(NonRetryableTelegramError("x", code="join_requested")) in _PENDING_MEMBERSHIP_ERRORS
    assert _warmup_error_key(NonRetryableTelegramError("x", code="channel_private")) in _UNAVAILABLE_GROUP_ERRORS
    assert _warmup_error_key(NonRetryableTelegramError("x", code="reaction_invalid")) in _REACTION_SKIP_ERRORS


class _MembershipHarness(TelegramMembershipMixin):
    def __init__(self, resolved_peer_id: int) -> None:
        self.resolved_peer_id = resolved_peer_id
        self.mutations: list[object] = []
        self.client = SimpleNamespace(get_input_entity=self._get_input_entity)

    async def _get_input_entity(self, _username):
        raw, peer_type = utils.resolve_id(self.resolved_peer_id)
        assert peer_type is types.PeerChannel
        return types.InputPeerChannel(raw, 12345)

    async def execute(self, method, *args, **kwargs):
        del kwargs
        if callable(method):
            return await method(*args)
        self.mutations.append(method)
        return SimpleNamespace()

    async def join(self, chat_id, *, dispatch_barrier=None):
        del dispatch_barrier
        self.mutations.append(chat_id)
        return True


@pytest.mark.asyncio
async def test_p07_username_reassignment_is_rejected_before_join() -> None:
    peer_x = int(utils.get_peer_id(types.PeerChannel(7001)))
    peer_y = int(utils.get_peer_id(types.PeerChannel(7002)))
    telegram = _MembershipHarness(peer_y)
    with pytest.raises(NonRetryableTelegramError) as captured:
        await telegram.join_saved_dialog(username="example", expected_peer_id=peer_x)
    assert captured.value.code == "join_target_identity_mismatch"
    assert telegram.mutations == []


@pytest.mark.asyncio
async def test_p07_matching_username_can_reach_join_boundary() -> None:
    peer_x = int(utils.get_peer_id(types.PeerChannel(7003)))
    telegram = _MembershipHarness(peer_x)
    assert await telegram.join_saved_dialog(username="example", expected_peer_id=peer_x)
    assert len(telegram.mutations) == 1


def test_p08_terminal_transport_callback_fires() -> None:
    seen: list[tuple[str, str]] = []
    transport = object.__new__(TelegramTransportMixin)
    transport._connected = True
    transport._terminal_account_error_callback = lambda code, message: seen.append((code, message))
    transport._notify_terminal_account_error("authorization_required", "revoked")
    assert transport._connected is False
    assert seen == [("authorization_required", "revoked")]
    wiring = (ROOT / "workers/handler_registry.py").read_text(encoding="utf-8")
    assert "terminal_account_error_callback=terminal_account_error" in wiring
    assert "mark_account_authorization_required(" in wiring
    assert 'cancellation("account", account_id)' in wiring


def test_p09_snapshot_is_atomic_and_scheduler_fails_closed(tmp_path: Path) -> None:
    db = Database(tmp_path / "campaign.db")
    _register(db, 303)
    db.select_telegram_account(303)

    before = db.get_latest_comment_campaign(account_id=303)
    with db.get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_snapshot BEFORE INSERT ON campaign_comment_settings
               BEGIN SELECT RAISE(ABORT, 'snapshot failure'); END"""
        )
    with pytest.raises(Exception):
        db.create_comment_campaign(
            ["hello"], daily_limit=1, slot_count=1, account_id=303,
            comment_settings_snapshot={"comment_source": "openai", "model": "gpt-test"},
        )
    after = db.get_latest_comment_campaign(account_id=303)
    assert (after or {}).get("id") == (before or {}).get("id")
    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER fail_snapshot")

    campaign = db.create_comment_campaign(
        ["hello"], daily_limit=1, slot_count=1, account_id=303,
        comment_settings_snapshot={"comment_source": "openai", "model": "gpt-test"},
    )
    settings = db.get_campaign_comment_settings(int(campaign["id"]))
    assert settings["snapshot_missing"] is False
    assert settings["comment_source"] == "openai"

    with db.get_connection() as conn:
        conn.execute("DELETE FROM campaign_comment_settings WHERE campaign_id=?", (int(campaign["id"]),))
        conn.execute("UPDATE comment_schedule SET scheduled_at=CURRENT_TIMESTAMP WHERE campaign_id=?", (int(campaign["id"]),))
    assert db.get_campaign_comment_settings(int(campaign["id"]))["snapshot_missing"] is True
    view = AccountCampaignDatabaseView(db, 303)
    assert view.queue_due_comment_slot() is None
    state = db.get_comment_campaign(int(campaign["id"]))
    assert state is not None and state["status"] == "paused"


def test_p11_failed_registration_restores_previous_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "registration.db")
    store = _MemorySecretStore()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    api = _AccountAPI(db, store, sessions)
    _register(db, 401, name="A")
    db.select_telegram_account(401)
    (sessions / "pending_aaaaaaaaaaaaaaaa.session").write_bytes(b"new")

    import services.api_parts.accounts as accounts_module
    real_update = accounts_module.update_account_lifecycle_journal

    def fail_final(secret_store, journal, *, phase, **updates):
        if phase == "committed":
            raise OSError("late final journal failure")
        return real_update(secret_store, journal, phase=phase, **updates)

    monkeypatch.setattr(accounts_module, "update_account_lifecycle_journal", fail_final)
    monkeypatch.setattr(
        "services.telegram_service.TelegramService._secure_session_file",
        staticmethod(lambda _path: None),
    )
    with pytest.raises(OSError, match="late final journal failure"):
        api.register_authorized_account(
            {"id": 402, "name": "B"},
            {"automation.enabled": True},
            pending_session_name="pending_aaaaaaaaaaaaaaaa",
        )
    assert db.get_telegram_account(402) is None
    assert db.get_selected_account_id() == 401
    assert db.get_setting("telegram.account_id") == "401"
    assert db.get_setting("telegram.account_name") == "A"


def test_p12_launcher_fast_path_and_silent_duplicate() -> None:
    launcher = (ROOT / "RUN_FROM_SOURCE_WINDOWS.ps1").read_text(encoding="utf-8-sig")
    direct = (ROOT / "RUN_FROM_SOURCE_WINDOWS_DIRECT_314.ps1").read_text(encoding="utf-8-sig")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert launcher.index("$fastMarkerValid") < launcher.index("$Python = Find-LansetSpBotPython")
    assert direct.index("$fastMarkerValid") < direct.index('Write-Step "Using the installed Python Launcher directly: py -3.14"')
    duplicate = main[main.index("        if not instance.acquire():"):main.index("        config = Config()")]
    assert "QMessageBox.information" not in duplicate


def test_p13_import_drops_foreign_access_hash(tmp_path: Path) -> None:
    db = Database(tmp_path / "transfer.db")
    _register(db, 501)
    _register(db, 502)
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO channels(
                   account_id, channel_id, username, title, target_kind,
                   comment_mode, access_hash, peer_type, created_at)
               VALUES(501, -1009001, 'chan', 'Channel', 'channel',
                      'channel_post', 987654321, 'channel', CURRENT_TIMESTAMP)"""
        )
    result = db.import_channels_between_accounts(source_account_id=501, target_account_id=502)
    assert result["imported"] == 1
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT access_hash, comment_mode FROM channels WHERE account_id=502 AND channel_id=-1009001"
        ).fetchone()
    assert row is not None
    assert row["access_hash"] is None
    assert row["comment_mode"] == "pending"


def test_p10_not_patched_account_wrapper_already_adds_stop_barrier() -> None:
    source = inspect.getsource(AccountQueueWorkerView.create_scope_dispatch_barrier)
    assert '("account", self.account_id)' in source
