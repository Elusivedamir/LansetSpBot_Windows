#!/usr/bin/env python3
"""Re-render the instruction screenshots from the running interface.

The images shipped in gui/assets/instructions are what the operator compares
against their own screen, so a stale one is worse than none: it teaches a
layout that no longer exists. They are captured from the real widgets here
rather than photographed by hand, so a rebuild after any UI change is one
command and the pictures cannot drift from the code.

Runs offscreen against a throwaway profile - no Telegram access, no writes
outside the temporary data directory.

Usage:
    python tools/capture_instruction_screenshots.py
    python tools/capture_instruction_screenshots.py --width 1440 --height 960

The default destination is ``dist/instruction-assets`` so an ordinary proof
run never edits committed source assets. Maintainers must pass an explicit
destination if they intentionally refresh a reviewed source fixture.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gui.instruction_assets import (  # noqa: E402 - project path is prepared above
    mark_instruction_assets_stale,
    write_instruction_asset_metadata,
)
from gui.resources import INSTRUCTION_ASSET_OVERRIDE_ENV  # noqa: E402

DESTINATION = PROJECT_ROOT / "dist" / "instruction-assets"

# Page index in LansetSpBotApp.stack -> screenshot name used by InstructionsView.
# Current MainWindow order:
# 0 Account, 1 Warmup, 2 Channels, 3 Links, 4 Commenting,
# 5 Target audience, 6 Audience parser, 7 Analytics, 8 Instructions.
PAGES = (
    (0, "01_account.png"),
    (2, "02_channels.png"),
    (3, "03_links.png"),
    (4, "04_comments.png"),
    (8, "05_instructions.png"),
)


def _prepare_windows_font_environment() -> Path | None:
    """Point Qt offscreen at real Windows fonts before QApplication exists."""

    if os.name != "nt":
        return None
    windows_root = Path(os.environ.get("WINDIR") or r"C:\Windows")
    font_dir = windows_root / "Fonts"
    if font_dir.is_dir():
        os.environ["QT_QPA_FONTDIR"] = str(font_dir)
        return font_dir
    return None


def _install_capture_font(application, font_dir: Path | None) -> str:
    """Load a Cyrillic-capable font explicitly for Qt's offscreen plugin."""

    from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics

    loaded_families: list[str] = []
    if font_dir is not None:
        for name in ("segoeui.ttf", "arial.ttf"):
            candidate = font_dir / name
            if not candidate.is_file():
                continue
            font_id = QFontDatabase.addApplicationFont(str(candidate))
            if font_id >= 0:
                loaded_families.extend(
                    QFontDatabase.applicationFontFamilies(font_id)
                )

    available = set(QFontDatabase.families())
    family = next(
        (
            candidate
            for candidate in ("Segoe UI", "Arial", "DejaVu Sans")
            if candidate in available or candidate in loaded_families
        ),
        "",
    )
    if not family:
        raise RuntimeError(
            "Instruction capture could not find a Cyrillic-capable GUI font"
        )

    font = QFont(family, 10)
    metrics = QFontMetrics(font)
    required = "ПрогревАккаунтКомментарииЯё"
    missing = [char for char in required if not metrics.inFontUcs4(ord(char))]
    if missing:
        raise RuntimeError(
            f"Instruction capture font {family!r} lacks Cyrillic glyphs: "
            + "".join(sorted(set(missing)))
        )
    application.setFont(font)
    return family


def _force_capture_font(window, family: str) -> None:
    """Override theme font lists only for build-time raster screenshots."""

    safe_family = family.replace('"', "")
    selectors = (
        "QWidget, QLabel, QPushButton, QLineEdit, QPlainTextEdit, QComboBox, "
        "QListWidget, QTableWidget, QCheckBox, QRadioButton, QAbstractSpinBox"
    )
    window.setStyleSheet(
        window.styleSheet()
        + f'\n{selectors} {{ font-family: "{safe_family}"; }}\n'
    )


def _assert_live_cyrillic_fonts(window) -> None:
    for label, widget in (
        ("account status", window.account_view.status_label),
        ("comment title", window.commenting_view.comments_title),
    ):
        metrics = widget.fontMetrics()
        if not all(metrics.inFontUcs4(ord(char)) for char in "ПриветЯё"):
            raise RuntimeError(
                f"Instruction capture widget {label} resolved to a font without Cyrillic"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--destination", default=str(DESTINATION))
    arguments = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    font_dir = _prepare_windows_font_environment()
    profile = Path(tempfile.mkdtemp(prefix="instruction-capture-"))
    os.environ["LANSETSPBOT_DATA_DIR"] = str(profile)
    os.environ["MARLEN_DATA_DIR"] = str(profile)
    destination = Path(arguments.destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    os.environ[INSTRUCTION_ASSET_OVERRIDE_ENV] = str(destination)
    # Mark before constructing InstructionsView so a previous successful build
    # cannot make this capture display old screenshots as current.
    mark_instruction_assets_stale(destination)

    from PySide6.QtWidgets import QApplication

    from core.composition import ApplicationContainer
    from core.config import Config
    from gui.app import LansetSpBotApp

    application = QApplication.instance() or QApplication(sys.argv)
    capture_font_family = _install_capture_font(application, font_dir)
    config = Config()
    container = ApplicationContainer(config)
    container.database.reset_running_tasks()
    # Multiaccount screenshot fixture: local rows only, no Telegram connection.
    for account_id, name, username, phone, state in (
        (910001, "Damir", "damir_main", "+79990001122", "running"),
        (910002, "Рабочий аккаунт", "work_account", "+79990003344", "connected"),
        (910003, "Резервный профиль", "reserve_account", "+79990005566", "paused"),
    ):
        container.database.register_telegram_account(
            telegram_account_id=account_id,
            session_name=f"account_{account_id}",
            display_name=name,
            username=username,
            phone=phone,
            authorized=True,
        )
        container.database.set_account_runtime_state(account_id, state)
    container.database.select_telegram_account(910001)
    container.database.select_telegram_account(910002)
    window = LansetSpBotApp(container.adapter, container.queue_worker, config)
    _force_capture_font(window, capture_font_family)
    window.resize(arguments.width, arguments.height)
    window.show()
    application.processEvents()
    _assert_live_cyrillic_fonts(window)

    # An empty activity log photographs as a broken panel. These lines are the
    # ones a first run actually produces, so the screenshot shows the panel
    # doing its job instead of a blank box. They are re-seeded immediately
    # before every capture: the panel clears its feed whenever it reconciles
    # the selected account, which happens on its own refresh timer.
    def seed_activity() -> None:
        append = getattr(window.activity_panel, "_append", None)
        if not callable(append):
            return
        for message in (
            "Telegram-аккаунт подключён.",
            "Сохранено каналов: 24.",
            "Связка готова: канал использует обсуждение, группа — прямое сообщение.",
            "Кампания запущена на 24 часа: запланировано 40 слотов.",
            "Комментарий отправлен, подтверждён Telegram.",
        ):
            append(message)
        application.processEvents()

    def prepare_page(index: int) -> None:
        # Show representative current controls before taking each page image.
        if index == 0:
            account = window.account_view
            account.api_id.setText("12345678")
            account.api_hash.setText("0123456789abcdef0123456789abcdef")
            account.phone.setText("+79990000000")
            account.proxy_enabled.setChecked(True)
            account.proxy_type.setCurrentText("SOCKS5")
            account.proxy_host.setText("proxy.example")
            account.proxy_port.setText("1080")
            account.schedule_enabled.setChecked(True)
            account.timezone_name.setText("Europe/Berlin")
        elif index == 4:
            comments = window.commenting_view
            comments.comment_source_combo.setCurrentIndex(1)
            comments.continuous.setChecked(True)
            if comments.editors:
                comments.editors[0].setPlainText(
                    "Спасибо за полезную публикацию — особенно важна практическая часть."
                )
        application.processEvents()

    written = 0
    try:
        for index, name in PAGES:
            if index >= window.stack.count():
                print(f"skipped {name}: page {index} does not exist")
                continue
            # Drive the sidebar, not the stack: selecting the row is what a user
            # does, and it leaves the correct entry highlighted. Setting the
            # stack directly left every screenshot showing "Аккаунт" selected.
            window.menu.setCurrentRow(index)
            window.stack.setCurrentIndex(index)
            prepare_page(index)
            for _ in range(3):
                application.processEvents()
            seed_activity()
            target = destination / name
            if not window.grab().save(str(target), "PNG"):
                print(f"could not write {target}")
                return 1
            written += 1
            print(f"{name}: {target.stat().st_size // 1024} KB")
        if written != len(PAGES):
            print(f"captured only {written} of {len(PAGES)} instruction pages")
            return 1
        write_instruction_asset_metadata(destination, PROJECT_ROOT)
    finally:
        try:
            window._tray.hide()  # noqa: SLF001 - controlled capture run
        except Exception:
            pass
        window.deleteLater()
        application.processEvents()
        container.shutdown(timeout_ms=15_000)

    print(f"{written} verified screenshots written to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
