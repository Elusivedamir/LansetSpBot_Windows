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
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=940)
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

    destination = Path(arguments.destination)
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        for index, name in PAGES:
            if index >= window.stack.count():
                print(f"skipped {name}: page {index} does not exist")
                continue
            window.stack.setCurrentIndex(index)
            for _ in range(3):
                application.processEvents()
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
