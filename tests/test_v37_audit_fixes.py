from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from gui.account_manager_panel import AccountManagerPanel
from services.api import ServiceAPI
from storage.database import Database
from workers.queue_worker import QueueWorker


def _account(account_id: int, name: str) -> dict:
    return {
        "telegram_account_id": account_id,
        "display_name": name,
        "username": name.lower(),
        "phone_masked": "+49 ***",
        "runtime_state": "connected",
        "authorized": True,
        "stopped": False,
        "campaign_active": False,
    }


def test_account_actions_are_disabled_while_selection_is_uncommitted() -> None:
    app = QApplication.instance() or QApplication([])
    panel = AccountManagerPanel()
    panel.reload(
        [_account(101, "A"), _account(202, "B")],
        selected_account_id=101,
        previous_account_id=0,
    )
    deleted: list[int] = []
    panel.delete_requested.connect(deleted.append)

    panel.selector.setCurrentIndex(panel.selector.findData(202))
    app.processEvents()
    assert panel.state_text.text() == "Переключение аккаунта…"
    assert panel.delete_button.isEnabled() is False
    panel._delete_clicked()
    assert deleted == []

    panel.set_selected_account_id(202)
    assert panel.delete_button.isEnabled() is True
    panel._delete_clicked()
    assert deleted == [202]
    panel.deleteLater()
    app.processEvents()


def test_hidden_selected_account_cannot_receive_destructive_action() -> None:
    app = QApplication.instance() or QApplication([])
    panel = AccountManagerPanel()
    panel.reload(
        [_account(101, "Alpha"), _account(202, "Bravo")],
        selected_account_id=101,
        previous_account_id=0,
    )
    stopped: list[int] = []
    panel.stop_requested.connect(stopped.append)

    panel.search.setText("bravo")
    app.processEvents()
    assert panel._selected_account_id == 101
    assert panel.selector.currentIndex() == -1
    assert panel.stop_button.isEnabled() is False
    panel._stop_clicked()
    assert stopped == []
    panel.deleteLater()
    app.processEvents()


def test_account_selection_has_global_ui_dispatch_barrier() -> None:
    account_view = Path("gui/views/account_view.py").read_text(encoding="utf-8")
    account_ops = Path("gui/views/account_parts/account_ops.py").read_text(encoding="utf-8")
    main = Path("gui/main_window.py").read_text(encoding="utf-8")
    assert "account_selection_busy = Signal(bool)" in account_view
    assert "self.account_selection_busy.emit(True)" in account_ops
    assert "self.account_selection_busy.emit(False)" in account_ops
    assert "self._durable_selected_account_id = account_id" in account_ops
    assert "load_settings(on_finished=self._finish_account_selection)" in account_ops
    settings = Path("gui/views/account_parts/settings.py").read_text(encoding="utf-8")
    assert "def load_settings(self, *, on_finished=None)" in settings
    assert "self.stack.setEnabled(enabled)" in main
    assert "self.activity_panel.setEnabled(enabled)" in main


def test_account_health_rejects_stale_results_and_replays_refresh() -> None:
    source = Path("gui/views/account_health_card.py").read_text(encoding="utf-8")
    assert "self._refresh_pending = False" in source
    assert "self._refresh_pending = True" in source
    assert source.count("if account_id != card._account_id():") >= 3
    # Deferred refreshes must use a card-owned child timer so they cannot fire
    # after Qt deleted the card; a static singleShot keeps the bound method
    # alive and crashed with "Internal C++ object already deleted".
    assert "QTimer.singleShot(0, card.refresh)" not in source
    assert "card._deferred_refresh_timer.start(0)" in source
    assert "self._deferred_refresh_timer = QTimer(self)" in source


def test_incremental_sync_is_classified_like_full_sync() -> None:
    source = Path("services/account_runtime_manager.py").read_text(encoding="utf-8")
    assert '"sync_new_channels"' in source[source.index("names = {") :]
    assert "sync_new_channels" in QueueWorker.IDEMPOTENT_TASK_TYPES
    assert "sync_new_channels" in QueueWorker.DIRECT_ACCOUNT_BOUND_TASK_TYPES
    assert "sync_new_channels" in QueueWorker.ACCOUNT_RPC_TASK_TYPES


def test_import_task_is_bound_to_selected_account(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    db = Database(tmp_path / "import-owner.db")
    db.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="A",
        authorized=True,
    )
    db.select_telegram_account(101)
    api = ServiceAPI(db, secret_migration_verified=True)
    task = api.create_task(
        "import",
        {"files": {"channels": "dummy.json"}},
    )
    api.prepare_shutdown()
    assert int(task["payload"]["account_id"]) == 101


def test_global_ui_theme_is_visible_with_selected_account(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    db = Database(tmp_path / "ui-theme.db")
    db.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="A",
        authorized=True,
    )
    db.select_telegram_account(101)
    db.set_setting("ui.theme", "light")
    api = ServiceAPI(db, secret_migration_verified=True)
    try:
        assert api.get_settings(prefix="ui.")["ui.theme"] == "light"
    finally:
        api.prepare_shutdown()


def test_direct_group_delivery_guard_is_account_scoped(tmp_path) -> None:
    db = Database(tmp_path / "direct-account-scope.db")
    task_a = db.insert_task(
        "direct_message",
        {"account_id": 101, "chat_id": -10055, "text": "A"},
    )
    task_b = db.insert_task(
        "direct_message",
        {"account_id": 202, "chat_id": -10055, "text": "B"},
    )
    task_a2 = db.insert_task(
        "direct_message",
        {"account_id": 101, "chat_id": -10055, "text": "A2"},
    )

    assert db.reserve_direct_message_delivery(
        task_a, -10055, "A", account_id=101
    )
    assert db.reserve_direct_message_delivery(
        task_b, -10055, "B", account_id=202
    )
    assert not db.reserve_direct_message_delivery(
        task_a2, -10055, "A2", account_id=101
    )
    assert int(db.get_direct_message_delivery(task_a)["account_id"]) == 101
    assert int(db.get_direct_message_delivery(task_b)["account_id"]) == 202


def test_v37_migration_backfills_direct_delivery_owner(tmp_path) -> None:
    path = tmp_path / "v36-direct.db"
    db = Database(path)
    task_id = db.insert_task(
        "direct_message",
        {"account_id": 303, "chat_id": 77, "text": "legacy"},
    )
    with db.get_connection() as conn:
        conn.execute("DROP TABLE direct_message_deliveries")
        conn.execute(
            """CREATE TABLE direct_message_deliveries(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   task_id INTEGER NOT NULL UNIQUE,
                   chat_id TEXT NOT NULL,
                   text TEXT NOT NULL,
                   message_id INTEGER,
                   status TEXT NOT NULL DEFAULT 'sending'
                       CHECK(status IN ('sending','sent','uncertain','failed')),
                   error TEXT,
                   reserved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        conn.execute(
            """INSERT INTO direct_message_deliveries(
                   task_id, chat_id, text, status
               ) VALUES(?, '77', 'legacy', 'uncertain')""",
            (task_id,),
        )
        conn.execute("DELETE FROM migrations WHERE version >= 37")
        conn.execute("PRAGMA user_version = 36")
    db.close_thread_connection()

    migrated = Database(path)
    assert migrated.get_version() == migrated.SCHEMA_VERSION
    delivery = migrated.get_direct_message_delivery(task_id)
    assert int(delivery["account_id"]) == 303


def test_account_delete_clears_global_dialog_provenance_and_direct_receipts(tmp_path) -> None:
    db = Database(tmp_path / "delete-account-v37.db")
    for account_id in (101, 202):
        db.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=f"A{account_id}",
            authorized=True,
        )
    dialog_id = db.upsert_saved_dialog(
        {
            "peer_id": -1009001,
            "username": "shared_peer",
            "title": "Shared peer",
            "kind": "group",
        },
        account_id=101,
        phone="+49123456789",
    )
    db.set_saved_dialog_membership(dialog_id, 202, "left")
    task_id = db.insert_task(
        "direct_message",
        {"account_id": 101, "chat_id": -1009001, "text": "x"},
    )
    assert db.reserve_direct_message_delivery(
        task_id, -1009001, "x", account_id=101
    )

    db.delete_telegram_account_data(101)

    with db.get_connection() as conn:
        dialog = conn.execute(
            "SELECT source_account_id, source_phone FROM saved_dialogs WHERE id=?",
            (dialog_id,),
        ).fetchone()
        receipt = conn.execute(
            "SELECT 1 FROM direct_message_deliveries WHERE task_id=?",
            (task_id,),
        ).fetchone()
    assert int(dialog["source_account_id"]) == 202
    assert dialog["source_phone"] is None
    assert receipt is None


def test_queue_task_signals_accept_64_bit_ids() -> None:
    app = QApplication.instance() or QApplication([])
    worker = QueueWorker(lambda: {})
    large = 5_000_000_123
    completed: list[int] = []
    failed: list[tuple[int, str]] = []
    worker.task_completed.connect(completed.append)
    worker.task_failed.connect(lambda task_id, message: failed.append((task_id, message)))
    worker.task_completed.emit(large)
    worker.task_failed.emit(large, "x")
    app.processEvents()
    assert completed == [large]
    assert failed == [(large, "x")]
