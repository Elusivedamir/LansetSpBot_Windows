from __future__ import annotations

import ast
import os
from pathlib import Path

from core.redaction import sanitize_text
from storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


def _extracted_method(path: Path, class_name: str, method_name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    original = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method = next(
        node
        for node in original.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )
    extracted = ast.ClassDef(
        name="Extracted",
        bases=[],
        keywords=[],
        body=[method],
        decorator_list=[],
    )
    module = ast.Module(body=[extracted], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"sanitize_text": sanitize_text}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["Extracted"]


def test_logged_out_campaign_queries_never_fall_back_to_previous_owner(tmp_path):
    db = Database(tmp_path / "campaign-isolation.db")
    db.set_setting("telegram.account_id", 101)
    comment = db.create_comment_campaign(
        ["private A text"],
        daily_limit=1,
        slot_count=1,
        continuous=False,
        account_id=101,
    )
    assert db.stop_comment_campaign(comment["id"])
    with db.get_connection() as conn:
        conn.execute(
            """INSERT INTO join_campaigns(
                   account_id,status,started_at,ends_at,max_per_hour,total_count,
                   created_at,updated_at)
               VALUES(101,'completed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1,0,
                      CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""
        )

    db.set_setting("telegram.account_id", "")
    assert db.get_active_comment_campaign() is None
    assert db.get_latest_comment_campaign() is None
    assert db.get_active_join_campaign() is None
    assert db.get_latest_join_campaign() is None
    assert db.get_latest_comment_campaign(account_id=101)["comments"] == [
        "private A text"
    ]
    assert int(db.get_latest_join_campaign(account_id=101)["account_id"]) == 101


def test_auth_error_redacts_namespaced_credentials():
    AuthWorker = _extracted_method(
        ROOT / "gui" / "auth_worker.py", "TelegramAuthWorker", "_safe_error_text"
    )
    worker = AuthWorker()
    worker.settings = {
        "telegram.api_hash": "RAW_API_HASH_123",
        "telegram.proxy_password": "RAW_PROXY_PASSWORD_456",
        "telegram.proxy_secret": "RAW_PROXY_SECRET_789",
        "telegram.phone": "+49123456789",
    }
    worker.code = "AUTH_CODE_111"
    worker.password = "TWO_FACTOR_222"
    worker.phone_code_hash = "PHONE_HASH_333"
    message = worker._safe_error_text(
        RuntimeError(
            "failed RAW_API_HASH_123 RAW_PROXY_PASSWORD_456 RAW_PROXY_SECRET_789 "
            "+49123456789 AUTH_CODE_111 TWO_FACTOR_222 PHONE_HASH_333"
        )
    )
    for secret in (
        "RAW_API_HASH_123",
        "RAW_PROXY_PASSWORD_456",
        "RAW_PROXY_SECRET_789",
        "+49123456789",
        "AUTH_CODE_111",
        "TWO_FACTOR_222",
        "PHONE_HASH_333",
    ):
        assert secret not in message


def test_stale_delivery_recovery_reports_each_real_owner(tmp_path):
    db = Database(tmp_path / "recovery-attribution.db")
    assert db.reserve_comment_delivery(10, 100, account_id=101, text="A")
    assert db.reserve_comment_delivery(20, 200, account_id=202, text="B")
    task_a = db.insert_task("direct_message", {"account_id": 101, "chat_id": 1})
    task_b = db.insert_task("direct_message", {"account_id": 202, "chat_id": 2})
    assert db.reserve_direct_message_delivery(task_a, 1, "A")
    assert db.reserve_direct_message_delivery(task_b, 2, "B")
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE comment_deliveries SET reserved_at=datetime('now','-10 minutes')"
        )
        conn.execute(
            "UPDATE direct_message_deliveries SET reserved_at=datetime('now','-10 minutes')"
        )

    recovered = db.recover_stale_deliveries(stale_after_seconds=300)
    assert recovered["total"] == 4
    assert recovered["accounts"] == {
        101: {"comment_deliveries": 1, "direct_message_deliveries": 1, "total": 2},
        202: {"comment_deliveries": 1, "direct_message_deliveries": 1, "total": 2},
    }


def test_gui_callbacks_are_bound_to_account_and_generation():
    commenting = (ROOT / "gui" / "views" / "commenting_view.py").read_text(
        encoding="utf-8"
    )
    activity = (ROOT / "gui" / "activity_panel.py").read_text(encoding="utf-8")
    channels = (ROOT / "gui" / "views" / "channels_view.py").read_text(
        encoding="utf-8"
    )
    links = (ROOT / "gui" / "views" / "links_view.py").read_text(encoding="utf-8")
    main = (ROOT / "gui" / "main_window.py").read_text(encoding="utf-8")

    assert "snapshot_account_id == self._current_account_id()" in commenting
    assert "snapshot_generation == self._account_generation" in commenting
    assert "self._refresh_pending = True" in commenting
    assert "current_account_id != snapshot_account_id" in activity
    assert "snapshot_generation != self._account_generation" in activity
    assert "self._reset_for_account(current_account_id)" in activity
    assert "generation != self._account_generation" in channels
    assert "self.watcher.stop()" in links
    for target in (
        "channels_view.handle_account_changed",
        "links_view.handle_account_changed",
        "commenting_view.handle_account_changed",
        "activity_panel.handle_account_changed",
    ):
        assert target in main


def test_sqlite_security_marker_detects_recreated_sidecar(tmp_path):
    path = tmp_path / "marker.db"
    db = Database(path)
    db.close_thread_connection()
    wal = Path(f"{path}-wal")
    wal.write_bytes(b"one")
    os.chmod(wal, 0o600)
    db._harden_database_artifacts(force=True)
    first_marker = db._artifact_security_markers.get(wal)
    if first_marker is None:
        # Filesystems without xattr/ADS support retain the inode/creation-time
        # fallback. The dedicated mocked Windows regression covers marker use.
        return
    assert db._read_artifact_security_marker(wal) == first_marker
    wal.unlink()
    wal.write_bytes(b"two")
    os.chmod(wal, 0o600)
    assert db._read_artifact_security_marker(wal) is None
    db._harden_database_artifacts()
    second_marker = db._artifact_security_markers.get(wal)
    assert second_marker is not None
    assert second_marker != first_marker


def test_disconnected_registered_account_can_be_selected_for_relogin(tmp_path):
    from storage.db_common import DatabaseError

    db = Database(tmp_path / "disconnect-state.db")
    row, created = db.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="Example",
        username="example",
        authorized=True,
    )
    assert created is True
    assert int(row["telegram_account_id"]) == 101
    db.select_telegram_account(101)

    state = db.mark_account_authorization_required(101)
    assert state["authorized"] is False
    assert state["stopped"] is True
    assert state["runtime_state"] == "authorization_required"
    assert db.get_selected_account_id() == 101
    assert str(db.get_setting("telegram.authorized", "")) == "0"

    import pytest
    with pytest.raises(DatabaseError):
        db.select_telegram_account(101)

    selected = db.select_telegram_account(101, allow_unauthorized=True)
    assert int(selected["telegram_account_id"]) == 101


def test_operator_requested_ui_ux_contracts_are_wired():
    from core.config import (
        DEFAULT_LINK_CHECK_DELAY_MAX_SECONDS,
        DEFAULT_LINK_CHECK_DELAY_MIN_SECONDS,
    )

    assert DEFAULT_LINK_CHECK_DELAY_MIN_SECONDS == 7
    assert DEFAULT_LINK_CHECK_DELAY_MAX_SECONDS == 12

    account = (ROOT / "gui" / "views" / "account_view.py").read_text(encoding="utf-8")
    manager = (ROOT / "gui" / "account_manager_panel.py").read_text(encoding="utf-8")
    warmup = (ROOT / "gui" / "views" / "warmup_view.py").read_text(encoding="utf-8")
    commenting = (ROOT / "gui" / "views" / "commenting_view.py").read_text(encoding="utf-8")
    instructions = (ROOT / "gui" / "views" / "instructions_view.py").read_text(encoding="utf-8")
    main = (ROOT / "gui" / "main_window.py").read_text(encoding="utf-8")

    assert 'QPushButton("Выйти из аккаунта")' in manager
    assert "disconnect_requested = Signal(int)" in manager
    assert "def _set_authorization_required_ui" in account
    assert "self._refresh_dynamic_layout(self.api_id)" in account
    assert '"Режим тишины · включён"' in account
    assert '"Режим тишины · выключен"' in account
    assert "self.existing_account_selector = QComboBox()" in warmup
    assert 'QLabel("Аккаунт A")' in warmup
    assert 'QLabel("Аккаунт B")' in warmup
    assert 'QPushButton("Комментарии")' in commenting
    assert 'QPushButton("Запуск кампании")' in commenting
    assert "scroll.ensureWidgetVisible(target, 28, 28)" in commenting
    assert "IMAGE_SHARE_OF_SLIDE = 0.78" in instructions
    assert "image.setMinimumHeight(300)" in instructions
    assert "self._theme_apply_timer.start(25)" in main
    assert "def _apply_pending_theme" in main
