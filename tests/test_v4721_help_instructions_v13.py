from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog, QLabel

from core.composition import ApplicationContainer
from core.config import Config
from core.version import BUILD_ID
from gui.app import MarlenApp
from gui.instruction_assets import METADATA_FILENAME
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


def test_instruction_slideshow_documents_current_gui_routes_and_help() -> None:
    app = _app()
    view = InstructionsView()

    assert view.stack.count() == len(view.STEPS)
    assert view.stack.count() >= 13

    comments_step = next(
        step for step in view.STEPS if step[0] == "Комментарии и источник текста"
    )
    assert "мешк" in comments_step[2]
    assert "ровно один раз" in comments_step[2]
    assert "светлый фон" in comments_step[2]
    assert "тёмный текст" in comments_step[2]

    account_step = next(
        step for step in view.STEPS if step[0] == "Смена Telegram-аккаунта"
    )
    assert "изолированы по Telegram account_id" in account_step[2]

    toggle_step = next(
        step for step in view.STEPS
        if step[0] == "Тихие часы и цвет переключателей"
    )
    assert "красный" in toggle_step[2]
    assert "зелёный" in toggle_step[2]
    assert "числовым регулятором" in toggle_step[2]

    route_step = next(
        step for step in view.STEPS if step[0] == "Запуск кампании и маршруты"
    )
    assert "обычную доступную" in route_step[2]
    assert "без привязки к посту" in route_step[2]
    assert "не создаёт вторую кампанию" in route_step[2]

    joined = "\n".join(step[0] + "\n" + step[2] for step in view.STEPS)
    assert "каждую секунду" in joined
    assert "личные переписки" in joined
    assert "Выполнено" in joined and "Отправлено" in joined
    assert "SOCKS5, SOCKS4 и HTTP" in joined
    assert "MTProxy" not in joined
    assert "LansetSpBot.exe" in joined

    assert "поддержка" in view.STEPS[-1][0].lower()
    assert "@lansetp" in view.STEPS[-1][2]

    for _title, asset, _text in view.STEPS:
        path = view._asset_path(asset)
        assert path.is_file(), f"{asset} is referenced but not shipped"
        assert path.stat().st_size > 10_000
    assert view._asset_path(METADATA_FILENAME).is_file()

    last_index = view.stack.count() - 1
    view.stack.setCurrentIndex(last_index)
    view._update_navigation()
    assert view.progress_label.text() == f"Шаг {last_index + 1} из {view.stack.count()}"
    assert view.next_button.isEnabled() is False
    view.deleteLater()
    app.processEvents()


def test_v13_build_identity_is_visible() -> None:
    assert BUILD_ID == "V46-OPENAI-PREMIUM-GUI"

def test_windows_build_regenerates_guide_assets_before_release_checks() -> None:
    root = Path(__file__).resolve().parents[1]
    build = (root / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8-sig"
    )
    capture = '& $BuildPython tools\\capture_instruction_screenshots.py'
    manifest = '& $BuildPython tools\\generate_manifest.py'
    pytest_gate = (
        "$BuildPython -X faulthandler -m coverage run "
        "--parallel-mode -m pytest"
    )
    package = "& $BuildPython -m PyInstaller"

    assert build.count(capture) == 1
    assert build.count(manifest) == 1
    assert build.index(capture) < build.index(manifest)
    assert build.index(manifest) < build.index(pytest_gate)
    assert build.index(manifest) < build.index(package)
    assert "Instruction screenshot regeneration failed." in build
    capture_source = (
        root / "tools" / "capture_instruction_screenshots.py"
    ).read_text(encoding="utf-8")
    assert "mark_instruction_assets_stale" in capture_source
    assert "write_instruction_asset_metadata" in capture_source
