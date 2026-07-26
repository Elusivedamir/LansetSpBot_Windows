from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from core.factory_reset import (
    FACTORY_RESET_JOURNAL_NAME,
    _create_rollback_snapshot,
    _managed_reset_targets,
    _write_reset_journal,
    recover_incomplete_factory_reset,
)
from core.paths import AppPaths
from gui.views.channels_view import ChannelTableModel, ChannelsView


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        database=root / "marlen.db",
        logs=root / "logs",
        sessions=root / "sessions",
        backups=root / "backups",
    )


def test_hard_crash_prepared_journal_restores_profile_before_database_creation(
    tmp_path,
):
    root = tmp_path / "profile"
    paths = _paths(root)
    paths.ensure()
    paths.database.write_bytes(b"ORIGINAL-DATABASE")
    (paths.sessions / "account.session").write_bytes(b"ORIGINAL-SESSION")
    (paths.logs / "marlen.log").write_text("ORIGINAL-LOG", encoding="utf-8")

    targets = _managed_reset_targets(
        database_path=paths.database, paths=paths, secret_path=root / ".secrets.json"
    )
    snapshot, entries = _create_rollback_snapshot(root, targets.all_targets)
    assert snapshot is not None
    _write_reset_journal(
        root=root, state="prepared", snapshot_path=snapshot, entries=entries
    )

    # Simulate os._exit after destructive work began: partial new profile remains.
    paths.database.unlink()
    (paths.sessions / "account.session").unlink()
    paths.database.write_bytes(b"PARTIAL-NEW-DATABASE")
    abandoned = root / ".sessions.factory-reset-crashed"
    (abandoned / "artifact").mkdir(parents=True)
    (abandoned / "artifact" / "leaked.session").write_bytes(b"DUPLICATE-SECRET")

    restored = recover_incomplete_factory_reset(
        database_path=paths.database, paths=paths, secret_path=root / ".secrets.json"
    )

    assert restored is True
    assert paths.database.read_bytes() == b"ORIGINAL-DATABASE"
    assert (paths.sessions / "account.session").read_bytes() == b"ORIGINAL-SESSION"
    assert (paths.logs / "marlen.log").read_text(encoding="utf-8") == "ORIGINAL-LOG"
    assert not (root / FACTORY_RESET_JOURNAL_NAME).exists()
    assert not list(root.glob(".factory-reset-rollback-*.tar"))
    assert not abandoned.exists()


def test_profile_rebuilt_journal_never_restores_old_profile(tmp_path):
    root = tmp_path / "profile"
    paths = _paths(root)
    paths.ensure()
    paths.database.write_bytes(b"OLD")
    targets = _managed_reset_targets(
        database_path=paths.database, paths=paths, secret_path=root / ".secrets.json"
    )
    snapshot, entries = _create_rollback_snapshot(root, targets.all_targets)
    assert snapshot is not None
    paths.database.write_bytes(b"NEW-EMPTY-PROFILE")
    _write_reset_journal(
        root=root, state="profile_rebuilt", snapshot_path=snapshot, entries=entries
    )

    restored = recover_incomplete_factory_reset(
        database_path=paths.database, paths=paths, secret_path=root / ".secrets.json"
    )

    assert restored is False
    assert paths.database.read_bytes() == b"NEW-EMPTY-PROFILE"
    assert not snapshot.exists()
    assert not (root / FACTORY_RESET_JOURNAL_NAME).exists()


def test_corrupt_prepared_journal_fails_closed_without_creating_database(tmp_path):
    root = tmp_path / "profile"
    paths = _paths(root)
    paths.ensure()
    journal = root / FACTORY_RESET_JOURNAL_NAME
    journal.write_text(
        json.dumps({"version": 1, "state": "prepared"}), encoding="utf-8"
    )

    import pytest
    from core.factory_reset import FactoryResetError

    with pytest.raises(FactoryResetError, match="rollback-снимок"):
        recover_incomplete_factory_reset(
            database_path=paths.database,
            paths=paths,
            secret_path=root / ".secrets.json",
        )
    assert not paths.database.exists()


class _LargeAdapter:
    def __init__(self, count: int = 50_000) -> None:
        self.count = count
        self.calls = 0

    def get_saved_dialogs(self):
        self.calls += 1
        return [
            {
                "peer_id": -(10_000 + index),
                "title": f"Channel {index}",
                "username": f"channel_{index}",
                "kind": "channel",
                "membership_status": "member",
            }
            for index in range(self.count)
        ]

    def get_channels(self):
        return []

    def get_join_campaign_state(self):
        return None

    def close_thread_connection(self):
        return None


def test_large_channel_list_uses_one_model_without_table_widget_items():
    app = QApplication.instance() or QApplication([])
    adapter = _LargeAdapter()
    view = ChannelsView(adapter)
    deadline = QEventLoop()

    def poll():
        if view.channel_model.rowCount() == adapter.count:
            deadline.quit()
        else:
            QTimer.singleShot(10, poll)

    QTimer.singleShot(10, poll)
    QTimer.singleShot(10_000, deadline.quit)
    deadline.exec()

    assert isinstance(view.channel_model, ChannelTableModel)
    assert view.channel_model.rowCount() == 50_000
    assert view.channel_model.peer_id_at(49_999) == -(10_000 + 49_999)
    assert not hasattr(view.table, "setItem")
    view.deleteLater()
    app.processEvents()
