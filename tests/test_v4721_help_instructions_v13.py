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

    assert view.stack.count() == 11
    # The comment-source page is located by title: the slideshow gained pages
    # over several releases, so a fixed index is not a stable contract.
    comments_step = next(
        step for step in view.STEPS if step[0] == "Комментарии и источник текста"
    )
    assert "одного–десяти" in comments_step[2]
    assert "перемешанный мешок" in comments_step[2]
    # Per-account isolation is documented on the account-switch page.
    account_step = next(
        step for step in view.STEPS if step[0] == "Смена Telegram-аккаунта"
    )
    assert "изолированы по Telegram account_id" in account_step[2]
    assert view.STEPS[-1][0] == "Ярлык, данные и поддержка"
    assert "@lansetp" in view.STEPS[-1][2]

    help_asset = view._asset_path("09_help.png")
    assert help_asset.is_file()
    assert help_asset.stat().st_size > 10_000

    # On the last page "next" must be disabled, whatever the page count is.
    last_index = view.stack.count() - 1
    view.stack.setCurrentIndex(last_index)
    view._update_navigation()
    assert view.progress_label.text() == f"Шаг {last_index + 1} из {view.stack.count()}"
    assert view.next_button.isEnabled() is False
    view.deleteLater()
    app.processEvents()


def test_v13_build_identity_is_visible() -> None:
    assert BUILD_ID == "V46-OPENAI-PREMIUM-GUI"
