from __future__ import annotations

import io
import logging
import sys

from PySide6.QtWidgets import QApplication

import main


def test_global_exception_hook_redacts_gui_and_raw_logger(monkeypatch) -> None:
    """Unhandled exceptions must be sanitized before every output boundary."""

    app = QApplication.instance() or QApplication([])
    app.setProperty("marlen_shutdown_in_progress", False)
    secret = "V24_GLOBAL_GUI_SECRET_7719"
    shown: list[str] = []
    monkeypatch.setattr(
        main.QMessageBox,
        "critical",
        lambda _parent, _title, message: shown.append(str(message)),
    )

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger(main.__name__)
    logger.addHandler(handler)
    previous_hook = sys.excepthook
    try:
        main._install_exception_hook()
        try:
            raise RuntimeError({"proxy_password": secret})
        except RuntimeError:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            assert exc_type is not None
            assert exc_value is not None
            sys.excepthook(exc_type, exc_value, exc_traceback)
    finally:
        sys.excepthook = previous_hook
        logger.removeHandler(handler)
        handler.close()

    assert shown
    combined = "\n".join(shown) + "\n" + stream.getvalue()
    assert secret not in combined
    assert "<redacted>" in combined
