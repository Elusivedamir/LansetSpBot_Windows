#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PAGES = (
    (0, "01_account.png", "Аккаунт"),
    (1, "02_warmup.png", "Прогрев"),
    (2, "03_channels.png", "Каналы"),
    (3, "04_links.png", "Связки"),
    (4, "05_comments.png", "Комментирование"),
    (5, "06_target_audience.png", "Поиск ЦА"),
    (6, "07_audience_parser.png", "Парсинг аудитории"),
    (7, "08_instructions.png", "Инструкция"),
)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--destination", default="dist/gui-tabs")
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    profile = Path(tempfile.mkdtemp(prefix="real-gui-capture-"))
    os.environ["LANSETSPBOT_DATA_DIR"] = str(profile)
    os.environ["MARLEN_DATA_DIR"] = str(profile)

    destination = (PROJECT_ROOT / args.destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from core.composition import ApplicationContainer
    from core.config import Config
    from gui.app import LansetSpBotApp

    application = QApplication.instance() or QApplication(sys.argv)
    # GitHub's offscreen Windows session can miss Qt's Cyrillic fallback.
    # Segoe UI is the ordinary Windows UI font; this affects only the capture.
    application.setFont(QFont("Segoe UI", 10))

    config = Config()
    container = ApplicationContainer(config)
    container.database.reset_running_tasks()

    for account_id, name, username, phone, state in (
        (910001, "Основной аккаунт", "main_account", "+79990001122", "running"),
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

    window = LansetSpBotApp(container.adapter, container.queue_worker, config)
    window.resize(args.width, args.height)
    window.show()
    application.processEvents()

    labels = tuple(window.menu.item(i).text() for i in range(window.menu.count()))
    expected = tuple(item[2] for item in PAGES)
    if labels != expected:
        raise RuntimeError(f"Unexpected menu: {labels!r}; expected {expected!r}")
    if window.stack.count() != len(PAGES):
        raise RuntimeError(f"Expected {len(PAGES)} pages, got {window.stack.count()}")

    try:
        for index, filename, label in PAGES:
            window.menu.setCurrentRow(index)
            window.stack.setCurrentIndex(index)
            for _ in range(8):
                application.processEvents()
            target = destination / filename
            if not window.grab().save(str(target), "PNG"):
                raise RuntimeError(f"Could not save {target}")
            print(f"{label}: {target.stat().st_size // 1024} KB")
    finally:
        try:
            window._tray.hide()
        except Exception:
            pass
        window.deleteLater()
        application.processEvents()
        container.shutdown(timeout_ms=15_000)

    print(f"Captured {len(PAGES)} real GUI tabs")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
