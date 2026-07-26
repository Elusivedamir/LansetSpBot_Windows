"""Single production entry point for LansetSpBot."""

from __future__ import annotations

import logging
import os
import shutil
import struct
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Packaged release builds use this mode during their post-build smoke test.
# It must be selected before importing Qt and before core.paths resolves paths.
_SELF_TEST = "--self-test" in sys.argv
_SELF_TEST_ROOT: Path | None = None
if _SELF_TEST:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _SELF_TEST_ROOT = Path(tempfile.mkdtemp(prefix="marlen-self-test-"))
    os.environ["MARLEN_DATA_DIR"] = str(_SELF_TEST_ROOT)

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.composition import ApplicationContainer  # noqa: E402
from core.config import Config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from core.redaction import sanitize_exception, sanitize_text  # noqa: E402
from core.factory_reset_runtime import (  # noqa: E402
    FACTORY_RESET_HELPER_FLAG,
    FACTORY_RESET_NO_RELAUNCH_FLAG,
    consume_factory_reset_result,
    launch_detached_factory_reset,
    recover_incomplete_factory_reset,
    run_factory_reset_helper,
)
from core.single_instance import SingleInstance  # noqa: E402
from core.version import APP_NAME, __version__  # noqa: E402
from gui.app import LansetSpBotApp  # noqa: E402

log = logging.getLogger(__name__)


def _resource_path(relative_path: str) -> Path:
    """Resolve a bundled resource in source and PyInstaller builds."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    base = Path(frozen_root) if frozen_root else Path(__file__).resolve().parent
    return base / relative_path


def _set_application_icon(app: QApplication) -> None:
    """Load the shared application/tray icon without making startup fragile."""

    icon_path = _resource_path("gui/assets/lansetspbot.png")
    if not icon_path.is_file():
        return
    icon = QIcon(str(icon_path))
    if not icon.isNull():
        app.setWindowIcon(icon)


def _set_windows_app_id() -> None:
    """Give the taskbar/tray a stable identity without adding a dependency."""
    if os.name != "nt":
        return
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        shell32 = getattr(windll, "shell32", None)
        set_app_id = getattr(shell32, "SetCurrentProcessExplicitAppUserModelID", None)
        if set_app_id is not None:
            set_app_id("com.marlen.pro")
    except Exception:
        # This is cosmetic and must never block application startup.
        logging.getLogger(__name__).debug("Could not set Windows AppUserModelID")


def _install_exception_hook() -> None:
    shown_signatures: set[str] = set()
    dialog_active = False

    def handle(exc_type, exc_value, exc_traceback):
        nonlocal dialog_active
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        text = sanitize_text(
            "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        )
        safe_exception = sanitize_exception(exc_value)
        log.critical("Unhandled application exception:\n%s", text)
        app = QApplication.instance()
        if app is None:
            return

        # Qt timers can continue to dispatch inside a modal message box.  A
        # failing periodic callback must therefore never open an unbounded chain
        # of identical critical dialogs, especially during shutdown/reset.
        if bool(app.property("marlen_shutdown_in_progress")):
            return
        signature = f"{exc_type.__name__}: {safe_exception}"
        if dialog_active or signature in shown_signatures:
            return

        shown_signatures.add(signature)
        dialog_active = True
        try:
            QMessageBox.critical(
                None,
                f"{APP_NAME} — критическая ошибка",
                f"Произошла непредвиденная ошибка:\n\n{safe_exception}\n\n"
                "Подробности записаны в marlen.log.",
            )
        finally:
            dialog_active = False

    sys.excepthook = handle


def _run_self_test(app: QApplication) -> int:
    """Initialize the real GUI/container and execute a no-network queue task.

    The release build script runs this once from source and once from the
    packaged application. It catches missing Qt plugins, broken hidden imports, an
    unwritable data directory, SQLite initialization errors and QThread startup
    failures without requesting Telegram credentials.
    """

    instance = SingleInstance(name=f"com.marlen.pro.selftest.{os.getpid()}")
    container: ApplicationContainer | None = None
    window: LansetSpBotApp | None = None
    try:
        setup_logging()
        if not instance.acquire():
            raise RuntimeError("self-test single-instance lock could not be acquired")

        config = Config()
        container = ApplicationContainer(config)
        container.database.reset_running_tasks()
        window = LansetSpBotApp(container.adapter, container.queue_worker, config)

        if window.stack.count() != 5:
            raise RuntimeError(f"unexpected GUI page count: {window.stack.count()}")
        if not config.paths.root.exists() or not config.database_path.parent.exists():
            raise RuntimeError("application data directory was not created")

        container.secret_store.set("self_test.value", "ok")
        if container.secret_store.get("self_test.value") != "ok":
            raise RuntimeError("secret store read/write check failed")
        container.secret_store.delete("self_test.value")

        task = container.api.create_task("noop", {"source": "packaged-self-test"})
        if not container.api.start_queue():
            raise RuntimeError("queue worker did not start")

        deadline = time.monotonic() + 10.0
        status = "pending"
        while time.monotonic() < deadline:
            app.processEvents()
            current = container.api.get_task(task["id"])
            status = str((current or {}).get("status", "missing"))
            if status in {"completed", "failed"}:
                break
            time.sleep(0.01)
        if status != "completed":
            raise RuntimeError(f"noop task did not complete: {status}")

        print(
            f"MARLEN_SELF_TEST_OK version={__version__} "
            f"platform={sys.platform} bits={8 * struct.calcsize('P')}",
            flush=True,
        )
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        if window is not None:
            try:
                window._tray.hide()  # noqa: SLF001 - controlled release smoke test
                window.deleteLater()
                app.processEvents()
            except Exception:
                pass
        if container is not None:
            try:
                container.shutdown(timeout_ms=15_000)
            except Exception:
                traceback.print_exc()
        instance.close()
        if _SELF_TEST_ROOT is not None:
            # Windows keeps log files locked until their handlers are closed.
            root_logger = logging.getLogger()
            for handler in list(root_logger.handlers):
                filename = getattr(handler, "baseFilename", "")
                try:
                    belongs_to_self_test = bool(
                        filename
                        and Path(filename)
                        .resolve()
                        .is_relative_to(_SELF_TEST_ROOT.resolve())
                    )
                except (OSError, ValueError):
                    belongs_to_self_test = False
                if belongs_to_self_test:
                    root_logger.removeHandler(handler)
                    handler.close()
            try:
                shutil.rmtree(_SELF_TEST_ROOT, ignore_errors=True)
            except Exception:
                pass


def _prepare_factory_reset_execution(container: ApplicationContainer) -> None:
    """Perform only non-blocking pre-handoff cancellation.

    Factory reset no longer waits for QThread, Telethon authorization, secret
    migration or QThreadPool jobs.  Those owners are terminated with the parent
    process, and the detached helper waits for that PID to disappear before it
    deletes any profile file.  Waiting here was the exact source of the disabled
    window that could not be closed.
    """

    api = getattr(container, "api", None)
    prepare_shutdown = getattr(api, "prepare_shutdown", None)
    if callable(prepare_shutdown):
        prepare_shutdown()


def _execute_factory_reset(_container: ApplicationContainer):
    """Schedule reset in a detached process that runs after this process exits."""

    return launch_detached_factory_reset(parent_pid=os.getpid())


def _terminate_after_factory_reset(exit_code: int) -> None:
    """End the live Qt process without waiting on threads whose profile is deleted.

    The detached helper is already running and waits for this PID to disappear.
    A normal Python/PySide teardown can block on Telethon/QThread cleanup and was
    the remaining source of the "application does not respond" loop.
    ``os._exit`` is limited to the confirmed factory-reset handoff path.
    """

    try:
        logging.shutdown()
    finally:
        os._exit(max(0, min(255, int(exit_code))))


def main() -> int:
    if FACTORY_RESET_HELPER_FLAG in sys.argv:
        try:
            index = sys.argv.index(FACTORY_RESET_HELPER_FLAG)
            parent_pid = int(sys.argv[index + 1])
        except (ValueError, IndexError) as exc:
            print(f"Invalid factory-reset helper arguments: {exc}", flush=True)
            return 2
        relaunch = FACTORY_RESET_NO_RELAUNCH_FLAG not in sys.argv
        return run_factory_reset_helper(parent_pid, relaunch=relaunch)

    _set_windows_app_id()
    app = QApplication(sys.argv)
    _set_application_icon(app)
    app.setQuitOnLastWindowClosed(False)
    app.setProperty("marlen_shutdown_in_progress", False)
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName("Lanset")
    QCoreApplication.setApplicationVersion(__version__)

    if _SELF_TEST:
        return _run_self_test(app)

    try:
        instance = SingleInstance()
    except Exception as exc:
        # Without the guard a second copy could start and fight the first one
        # over the database and the Telegram session, so a broken profile has
        # to stop startup with an explanation instead of a bare traceback.
        safe_exception = sanitize_exception(exc)
        QMessageBox.critical(
            None,
            f"{APP_NAME} — профиль недоступен",
            "Не удалось подготовить каталог данных, поэтому защита от "
            "повторного запуска не работает и программа не будет запущена.\n\n"
            f"{safe_exception}\n\n"
            "Проверьте права на папку профиля, затем запустите снова.",
        )
        return 1
    container: ApplicationContainer | None = None
    window: LansetSpBotApp | None = None
    instance_closed = False
    exit_code = 1
    try:
        if not instance.acquire():
            # The primary window has been asked to come forward. Say so anyway:
            # a launch that appears to do nothing is what made operators start
            # the program again and again.
            log.info("Another LansetSpBot instance is already running")
            QMessageBox.information(
                None,
                APP_NAME,
                "LansetSpBot уже запущен.\n\n"
                "Открыто существующее окно — второй копии не будет: две копии "
                "работали бы с одной базой и одной Telegram-сессией.",
            )
            return 0

        config = Config()
        recovery_result = recover_incomplete_factory_reset(config)

        try:
            setup_logging()
        except Exception as exc:
            safe_exception = sanitize_exception(exc)
            logging.basicConfig(level=logging.ERROR)
            logging.error(
                "Logging/data directory initialization failed: %s", safe_exception
            )
            QMessageBox.critical(
                None,
                f"{APP_NAME} — ошибка каталога данных",
                f"Не удалось создать защищённый каталог данных:\n\n{safe_exception}",
            )
            return 1

        _install_exception_hook()
        if recovery_result is not None:
            QMessageBox.warning(
                None,
                f"{APP_NAME} — профиль восстановлен",
                str(
                    recovery_result.get("message") or "Незавершённый сброс восстановлен"
                ),
            )

        factory_reset_result = consume_factory_reset_result(config)
        if factory_reset_result is not None:
            # Display the helper result before constructing ApplicationContainer.
            # Creating the container starts database maintenance, campaign timers,
            # secret migration and potentially Telegram work.
            ok = bool(factory_reset_result.get("ok"))
            message = str(
                factory_reset_result.get("message")
                or ("Заводской сброс завершён" if ok else "Заводской сброс не выполнен")
            )
            if ok:
                removed_files = int(factory_reset_result.get("removed_files", 0) or 0)
                removed_directories = int(
                    factory_reset_result.get("removed_directories", 0) or 0
                )
                QMessageBox.information(
                    None,
                    f"{APP_NAME} — сброс завершён",
                    f"{message}\n\nУдалено файлов: {removed_files}; "
                    f"каталогов: {removed_directories}.",
                )
            else:
                QMessageBox.critical(
                    None,
                    f"{APP_NAME} — ошибка сброса",
                    message,
                )

        container = ApplicationContainer(config)
        container.database.reset_running_tasks()
        window = LansetSpBotApp(
            container.adapter,
            container.queue_worker,
            config,
            factory_reset_preparer=lambda: _prepare_factory_reset_execution(container),
            factory_reset_executor=lambda: _execute_factory_reset(container),
            factory_reset_parent_terminator=None,
            shutdown_finalizer=container.finalize_shutdown,
        )
        instance.activation_requested.connect(window.show_from_tray)
        window.show()
        exit_code = int(app.exec())

        return exit_code
    except Exception as exc:
        safe_exception = sanitize_exception(exc)
        log.critical("Fatal startup error:\n%s", sanitize_text(traceback.format_exc()))
        QMessageBox.critical(
            None,
            f"{APP_NAME} — ошибка запуска",
            f"Приложение не удалось запустить:\n\n{safe_exception}\n\n"
            "Подробности записаны в marlen.log.",
        )
        return 1
    finally:
        reset_handoff = bool(
            window is not None
            and getattr(window, "factory_reset_handoff_scheduled", False)
        )
        if reset_handoff:
            # The helper cannot begin until this PID is gone.  Do not call the
            # normal 60-second container shutdown here: a stuck Telegram/QThread
            # owner would keep the disabled Qt window alive and make the reset
            # appear frozen forever.  The profile is intentionally destroyed by
            # the helper after this immediate process exit.
            log.info("Factory reset handoff committed; terminating parent process")
            if not instance_closed:
                instance.close()
                instance_closed = True
            _terminate_after_factory_reset(exit_code)

        runtime_shutdown_finalized = bool(
            window is not None
            and getattr(window, "runtime_shutdown_finalized", False)
        )
        if container is not None:
            if not runtime_shutdown_finalized:
                container.shutdown(timeout_ms=60_000)
        if not instance_closed:
            instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
