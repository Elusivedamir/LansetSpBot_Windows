from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog, QLabel

from core.composition import ApplicationContainer
from core.config import Config
from core.version import BUILD_ID
from gui.app import MarlenApp
from gui.views.instructions_view import InstructionsView


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_sidebar_help_button_is_small_last_item_and_shows_support_contact(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "help.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)

    assert window.help_button.text() == "Помощь"
    assert window.help_button.objectName() == "sidebarHelpButton"
    assert window.help_button.toolTip() == "Контакт поддержки: @lansetp"
    assert window.SUPPORT_CONTACT == "@lansetp"
    assert window.SUPPORT_URL == "https://t.me/lansetp"
    assert window.sidebar_layout.indexOf(
        window.help_button
    ) > window.sidebar_layout.indexOf(window.version_label)

    captured: list[list[str]] = []

    def fake_exec(dialog: QDialog) -> int:
        captured.append([label.text() for label in dialog.findChildren(QLabel)])
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window.help_button.click()
    assert captured
    assert any("@lansetp" in text for text in captured[0])

    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_instruction_slideshow_documents_v12_comment_bag_and_help() -> None:
    app = _app()
    view = InstructionsView()

    assert view.stack.count() == 9
    assert view.STEPS[3][0] == "Комментарии и суточная нагрузка"
    assert "от одного до десяти" in view.STEPS[3][2]
    assert "перемешанный мешок" in view.STEPS[3][2]
    assert "предыдущего Telegram-аккаунта" in view.STEPS[3][2]
    assert view.STEPS[-1][0] == "Ярлык, данные и поддержка"
    assert "@lansetp" in view.STEPS[-1][2]

    help_asset = view._asset_path("09_help.png")
    assert help_asset.is_file()
    assert help_asset.stat().st_size > 10_000

    view.stack.setCurrentIndex(8)
    view._update_navigation()
    assert view.progress_label.text() == "Шаг 9 из 9"
    assert view.next_button.isEnabled() is False
    view.deleteLater()
    app.processEvents()


def test_v13_build_identity_is_visible() -> None:
    assert BUILD_ID == "V43-SENIOR-AUDIT-ACCOUNT-ISOLATION-FIXES"
