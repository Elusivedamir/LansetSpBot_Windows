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
