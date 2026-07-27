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

DESTINATION = PROJECT_ROOT / "gui" / "assets" / "instructions"

# Page index in LansetSpBotApp.stack -> screenshot name used by InstructionsView.
PAGES = (
    (0, "01_account.png"),
    (1, "02_channels.png"),
    (2, "03_links.png"),
    (3, "04_comments.png"),
    (4, "05_instructions.png"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--destination", default=str(DESTINATION))
    arguments = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    profile = Path(tempfile.mkdtemp(prefix="instruction-capture-"))
    os.environ["MARLEN_DATA_DIR"] = str(profile)

    from PySide6.QtWidgets import QApplication

    from core.composition import ApplicationContainer
    from core.config import Config
    from gui.app import LansetSpBotApp

    application = QApplication.instance() or QApplication(sys.argv)
    config = Config()
    container = ApplicationContainer(config)
    container.database.reset_running_tasks()
    window = LansetSpBotApp(container.adapter, container.queue_worker, config)
    window.resize(arguments.width, arguments.height)
    window.show()
    application.processEvents()

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
        elif index == 3:
            comments = window.commenting_view
            comments.comment_source_combo.setCurrentIndex(1)
            comments.continuous.setChecked(True)
            if comments.editors:
                comments.editors[0].setPlainText(
                    "Спасибо за полезную публикацию — особенно важна практическая часть."
                )
        application.processEvents()

    destination = Path(arguments.destination)
    destination.mkdir(parents=True, exist_ok=True)
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
    finally:
        try:
            window._tray.hide()  # noqa: SLF001 - controlled capture run
        except Exception:
            pass
        window.deleteLater()
        application.processEvents()
        container.shutdown(timeout_ms=15_000)

    print(f"{written} screenshots written to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
