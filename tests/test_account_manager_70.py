from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from gui.account_manager_panel import AccountManagerPanel


def _accounts(count: int) -> list[dict]:
    return [
        {
            "telegram_account_id": index,
            "display_name": f"Account {index}",
            "username": f"user{index}",
            "phone_masked": f"+7 *** ***-{index:04d}"[-17:],
            "runtime_state": "connected",
            "stopped": False,
            "campaign_active": False,
        }
        for index in range(1, count + 1)
    ]


def test_listing_account_catalog_does_not_create_telegram_runtimes() -> None:
    source = Path("services/api_parts/accounts.py").read_text(encoding="utf-8")
    start = source.index("    def list_telegram_accounts(")
    end = source.index("    def get_selected_account_id(", start)
    block = source[start:end]
    assert "database.list_telegram_accounts" in block
    assert "get_runtime" not in block
    assert "TelegramService" not in block


def test_catalog_filters_seventy_accounts_without_changing_selection() -> None:
    app = QApplication.instance() or QApplication([])
    panel = AccountManagerPanel()
    emitted: list[int] = []
    panel.account_selected.connect(emitted.append)
    panel.reload(
        _accounts(70),
        selected_account_id=42,
        previous_account_id=41,
    )
    assert panel.counter.text() == "Подключено 70 из 70 аккаунтов"
    assert panel.add_button.isEnabled() is False
    assert panel.selector.count() == 70

    panel.search.setText("user7")
    app.processEvents()
    assert emitted == []
    assert panel._selected_account_id == 42
    assert panel.selector.count() == 2  # user7 and user70
    assert panel.state_text.text() == "Подключён"

    panel.search.setText("42")
    app.processEvents()
    assert panel.selector.count() == 1
    assert int(panel.selector.currentData()) == 42
    assert emitted == []

    panel.search.clear()
    app.processEvents()
    assert panel.selector.count() == 70
    assert int(panel.selector.currentData()) == 42
    panel.deleteLater()
    app.processEvents()

def test_account_manager_supports_telegram_ids_above_qt_int32() -> None:
    app = QApplication.instance() or QApplication([])
    panel = AccountManagerPanel()
    large_id = 5_173_126_087
    account = {
        "telegram_account_id": large_id,
        "display_name": "Large ID Account",
        "username": "large_id_user",
        "phone_masked": "+7 *** ***-0001",
        "runtime_state": "connected",
        "authorized": True,
        "stopped": False,
        "campaign_active": False,
    }
    selected, stopped, resumed = [], [], []
    reauthorized, disconnected, deleted = [], [], []
    panel.account_selected.connect(selected.append)
    panel.stop_requested.connect(stopped.append)
    panel.resume_requested.connect(resumed.append)
    panel.reauthorize_requested.connect(reauthorized.append)
    panel.disconnect_requested.connect(disconnected.append)
    panel.delete_requested.connect(deleted.append)
    panel.reload([account], selected_account_id=large_id, previous_account_id=0)
    panel.selector.setCurrentIndex(-1)
    panel.selector.setCurrentIndex(0)
    app.processEvents()
    assert selected == [large_id]
    panel._stop_clicked()
    panel._resume_clicked()
    panel._reauthorize_clicked()
    panel._disconnect_clicked()
    panel._delete_clicked()
    assert stopped == [large_id]
    assert resumed == [large_id]
    assert reauthorized == [large_id]
    assert disconnected == [large_id]
    assert deleted == [large_id]
    panel.deleteLater()
    app.processEvents()
