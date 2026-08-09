"""Qt application controller and system-tray lifecycle."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QStyle,
    QSystemTrayIcon,
)

from core.config import Config
from core.version import APP_NAME
from gui.background import BackgroundCall
from .main_window import MainWindow

log = logging.getLogger(__name__)


class LansetSpBotApp(MainWindow):
    """Main window with tray lifecycle and non-destructive graceful shutdown."""

    quit_requested = Signal()

    def __init__(
        self,
        adapter,
        queue_worker=None,
        config: Optional[Config] = None,
        *,
        factory_reset_preparer: Callable[[], object] | None = None,
        factory_reset_executor: Callable[[], object] | None = None,
        factory_reset_logging_reinitializer: Callable[[], object] | None = None,
        factory_reset_parent_terminator: Callable[[], object] | None = None,
        shutdown_finalizer: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(adapter, queue_worker, config)
        self.config = config or Config()
        self.queue_worker = queue_worker
        self._factory_reset_preparer = factory_reset_preparer
        self._factory_reset_executor = factory_reset_executor
        self._factory_reset_logging_reinitializer = factory_reset_logging_reinitializer
        self._factory_reset_parent_terminator = factory_reset_parent_terminator
        self._shutdown_finalizer = shutdown_finalizer
        self._quitting = False
        self._allow_qt_quit = False
        self._quit_finalize_started = False
        self._runtime_shutdown_finalized = False
        self._factory_reset_pending = False
        self._shutdown_deadline = 0.0
        self._factory_reset_job: BackgroundCall | None = None
        self._factory_reset_helper_pid: int | None = None
        self._shutdown_progress: QProgressDialog | None = None
        self.setWindowTitle(APP_NAME)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        self._tray = self._create_tray()
        self._install_shortcuts()

        self._keep_alive_timer = QTimer(self)
        self._keep_alive_timer.setInterval(30_000)
        self._keep_alive_timer.timeout.connect(self._keep_alive_tick)
        self._keep_alive_timer.start()

        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(100)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)
        self.quit_requested.connect(self.account_view.request_auth_stop)
        self.account_view.factory_reset_requested.connect(self.request_factory_reset)

    def _create_tray(self) -> QSystemTrayIcon:
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.setWindowIcon(icon)

        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip(APP_NAME)

        menu = QMenu(self)
        minimize_action = QAction("Свернуть окно", menu)
        minimize_action.triggered.connect(self.showMinimized)
        menu.addAction(minimize_action)
        show_action = QAction("Открыть LansetSpBot", menu)
        show_action.triggered.connect(self.show_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        quit_action = QAction("Выход", menu)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(quit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray.show()
        else:
            log.warning("System tray is not available on this platform/session")
        return tray

    def _install_shortcuts(self) -> None:
        # Ctrl+W goes through close(), so it asks like the window button does
        # instead of silently hiding a still-running application.
        self._close_shortcut = QShortcut(QKeySequence.StandardKey.Close, self)
        self._close_shortcut.activated.connect(self.close)
        self._quit_shortcut = QShortcut(QKeySequence.StandardKey.Quit, self)
        self._quit_shortcut.activated.connect(self.quit_application)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

    def show_from_tray(self) -> None:
        if self._quitting:
            if self._tray.isVisible():
                self._tray.showMessage(
                    APP_NAME,
                    "Идёт безопасное завершение фоновых операций. Дождитесь результата.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
            return
        self.show()
        self.raise_()
        self.activateWindow()

    @property
    def factory_reset_pending(self) -> bool:
        return self._factory_reset_pending

    @property
    def factory_reset_handoff_scheduled(self) -> bool:
        """Whether a detached helper owns the reset after this process exits."""

        return bool(self._factory_reset_helper_pid)

    @property
    def runtime_shutdown_finalized(self) -> bool:
        """Whether the normal runtime was closed before leaving the Qt loop."""

        return bool(self._runtime_shutdown_finalized)

    @Slot()
    def request_factory_reset(self) -> None:
        if self._quitting:
            return
        self._factory_reset_pending = True
        self.account_view.set_factory_reset_pending(True)
        self._begin_shutdown(factory_reset=True)

    def quit_application(self) -> None:
        self._begin_shutdown(factory_reset=False)

    def _show_shutdown_progress(self, *, factory_reset: bool) -> None:
        """Show a responsive, cancellable progress window during shutdown.

        Factory reset used to execute the complete filesystem/SQLite rebuild on
        the GUI thread after disabling the main window.  That looked
        indistinguishable from an application hang and could trigger the system
        "not responding" state while a rollback archive was being written.  A
        top-level progress dialog keeps the Qt event loop visibly alive while the
        destructive phase runs in LansetSpBot's worker pool.
        """

        if self._shutdown_progress is not None:
            self._shutdown_progress.setLabelText(
                "Подготовка заводского сброса…"
                if factory_reset
                else "Безопасное завершение работы…"
            )
            return

        dialog = QProgressDialog(
            "Подготовка заводского сброса…"
            if factory_reset
            else "Безопасное завершение работы…",
            "Отменить",
            0,
            0,
            self,
        )
        dialog.setWindowTitle(APP_NAME)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)
        dialog.setMinimumWidth(420)
        dialog.canceled.connect(self._cancel_shutdown_from_progress)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._shutdown_progress = dialog

    def _set_shutdown_progress_text(self, text: str) -> None:
        dialog = self._shutdown_progress
        if dialog is not None:
            dialog.setLabelText(str(text))

    def _close_shutdown_progress(self) -> None:
        dialog = self._shutdown_progress
        self._shutdown_progress = None
        if dialog is not None:
            dialog.blockSignals(True)
            dialog.hide()
            dialog.close()
            dialog.deleteLater()

    @Slot()
    def _cancel_shutdown_from_progress(self) -> None:
        """Restore the live application while no detached reset was scheduled."""

        if not self._quitting or self._factory_reset_job is not None:
            return
        self._shutdown_timer.stop()
        if self._factory_reset_pending:
            message = (
                "Заводской сброс отменён. Кампании, остановленные при подготовке, "
                "не запускаются автоматически."
            )
            blockers = ["factory_reset_cancelled"]
        else:
            message = "Выход отменён. Приложение продолжает работу."
            blockers = ["shutdown_cancelled"]
        self._abort_shutdown(
            message,
            blockers=blockers,
            critical=False,
        )

    def _begin_shutdown(self, *, factory_reset: bool) -> None:
        """Request shutdown and quit only after every LansetSpBot-owned job has stopped."""
        if self._quitting:
            return
        self._quitting = True
        app = QApplication.instance()
        if app is not None:
            app.setProperty("marlen_shutdown_in_progress", True)
        self._show_shutdown_progress(factory_reset=factory_reset)
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(False)
        self.quit_requested.emit()
        self._keep_alive_timer.stop()

        try:
            if factory_reset:
                # Persistent schedules belong to the local profile being deleted.
                # Stop them automatically instead of requiring the user to visit
                # two other tabs before the reset button is accepted.
                self.adapter.prepare_factory_reset()
            else:
                self.adapter.prepare_shutdown()
        except Exception as exc:
            log.exception("Could not prepare application shutdown")
            self._abort_shutdown(
                "Не удалось подготовить безопасное завершение:\n\n"
                f"{type(exc).__name__}: {exc}",
                blockers=["shutdown_prepare_failed"],
            )
            return

        if factory_reset:
            # A factory reset intentionally destroys the complete local profile.
            # Waiting for every Qt/Telethon/background owner before even starting
            # the detached helper can deadlock the GUI (and makes closeEvent ignore
            # every close request while ``_quitting`` is true).  Schedule the helper
            # immediately after cooperative cancellation.  The main entry point
            # then performs a bounded hard process exit; the helper touches files
            # only after the parent PID is gone.
            self._complete_shutdown()
            return

        blockers = self._background_shutdown_blockers()
        if blockers:
            self._set_shutdown_progress_text("Остановка фоновых операций…")
            self._shutdown_deadline = time.monotonic() + 45.0
            self._shutdown_timer.start()
            if self._shutdown_progress is not None:
                self._shutdown_progress.raise_()
                self._shutdown_progress.activateWindow()
            if self._tray.isVisible():
                self._tray.showMessage(
                    APP_NAME,
                    "Завершение текущей операции…",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
            log.info("Waiting for shutdown blockers: %s", ", ".join(blockers))
            return
        self._complete_shutdown()

    def _complete_shutdown(self) -> None:
        """Commit a reset only after blockers stop; otherwise restore the live app."""

        if not self._factory_reset_pending:
            self._finalize_quit()
            return

        executor = self._factory_reset_executor
        if executor is None:
            self._abort_shutdown(
                "Заводской сброс не запущен: обработчик удаления локальных данных "
                "не был создан.",
                blockers=["factory_reset_executor_missing"],
            )
            return

        suspend_updates = getattr(self, "suspend_runtime_updates", None)
        if callable(suspend_updates):
            suspend_updates()

        preparer = getattr(self, "_factory_reset_preparer", None)
        if callable(preparer):
            self._set_shutdown_progress_text(
                "Закрытие базы данных и подготовка локального профиля…"
            )
            try:
                preparer()
            except Exception as exc:
                log.exception("Could not finalize owners before factory reset")
                self._abort_shutdown(
                    "Не удалось подготовить локальный профиль к удалению. "
                    "Данные не были сброшены.\n\n"
                    f"{type(exc).__name__}: {exc}",
                    blockers=["factory_reset_prepare_failed"],
                )
                return

        # Keep the historical method seam for unit-test doubles, but the real
        # implementation now performs only a synchronous Popen handoff.  No
        # QRunnable/QThreadPool job participates in factory-reset shutdown.
        handoff_starter = getattr(self, "_start_factory_reset_async", None)
        if callable(handoff_starter):
            handoff_starter()
            return

        set_progress = getattr(self, "_set_shutdown_progress_text", None)
        if callable(set_progress):
            set_progress("Запуск отдельного процесса сброса…\nLansetSpBot сейчас закроется.")
        progress = getattr(self, "_shutdown_progress", None)
        if progress is not None:
            progress.setCancelButton(None)

        try:
            result = executor()
        except Exception as exc:
            profile_restored = getattr(exc, "profile_restored", None)
            if profile_restored is False:
                log.critical(
                    "Factory reset rollback was incomplete; closing unsafe application",
                    exc_info=True,
                )
                self._factory_reset_pending = False
                self.account_view.set_factory_reset_pending(False)
                QMessageBox.critical(
                    self,
                    f"{APP_NAME} — восстановление не завершено",
                    "Сброс завершился ошибкой, и локальный профиль не удалось "
                    "восстановить полностью. Чтобы не продолжать работу с "
                    "несогласованными данными, приложение будет закрыто.\n\n"
                    f"{type(exc).__name__}: {exc}",
                )
                self._finalize_quit()
                return
            log.exception("Factory reset failed; restoring interactive application")
            self._abort_shutdown(
                "Не удалось удалить локальные данные. Файловый профиль "
                "восстановлен, приложение продолжает работу. Активные кампании "
                "остались остановленными и не возобновлены автоматически из "
                "соображений безопасности.\n\n"
                f"{type(exc).__name__}: {exc}",
                blockers=["factory_reset_failed"],
            )
            return

        removed_files = int(getattr(result, "removed_files", 0) or 0)
        removed_directories = int(getattr(result, "removed_directories", 0) or 0)
        QMessageBox.information(
            self,
            f"{APP_NAME} — сброс завершён",
            "Все локальные данные удалены. При следующем запуске программа "
            "будет работать как после первой установки.\n\n"
            f"Удалено файлов: {removed_files}; каталогов: {removed_directories}.",
        )
        self._finalize_quit()

    def _start_factory_reset_async(self) -> None:
        """Schedule the detached helper without involving QThreadPool.

        The operation is only an internal ``subprocess.Popen`` and returns
        immediately.  Running it directly removes the pending-job/deadlock seam
        observed while the actual destructive work remains detached.
        """

        if self._factory_reset_job is not None:
            return
        executor = self._factory_reset_executor
        if executor is None:
            self._abort_shutdown(
                "Заводской сброс не запущен: обработчик удаления локальных данных "
                "не был создан.",
                blockers=["factory_reset_executor_missing"],
            )
            return

        self._set_shutdown_progress_text(
            "Запуск отдельного процесса сброса…\nLansetSpBot сейчас закроется."
        )
        if self._shutdown_progress is not None:
            self._shutdown_progress.setCancelButton(None)

        try:
            outcome: tuple[bool, object] = (True, executor())
        except Exception as exc:  # noqa: BLE001 - delivered through one GUI boundary
            outcome = (False, exc)
        self._on_factory_reset_outcome(outcome)

    @Slot(object)
    def _on_factory_reset_outcome(self, outcome: object) -> None:
        self._factory_reset_job = None
        try:
            succeeded, payload = outcome  # type: ignore[misc]
        except Exception:
            succeeded = False
            payload = RuntimeError(
                f"Некорректный результат заводского сброса: {outcome!r}"
            )

        reinitializer = self._factory_reset_logging_reinitializer
        if callable(reinitializer):
            try:
                reinitializer()
            except Exception as exc:
                self._close_shutdown_progress()
                self._factory_reset_pending = False
                self.account_view.set_factory_reset_pending(False)
                QMessageBox.critical(
                    self,
                    f"{APP_NAME} — ошибка восстановления журнала",
                    "Локальный профиль был обработан, но файловый журнал не удалось "
                    "запустить заново. Приложение будет закрыто, чтобы не продолжать "
                    "работу в непроверенном состоянии.\n\n"
                    f"{type(exc).__name__}: {exc}",
                )
                self._finalize_quit()
                return

        if not bool(succeeded):
            if isinstance(payload, BaseException):
                self._handle_factory_reset_failure(payload)
            else:
                self._handle_factory_reset_failure(RuntimeError(str(payload)))
            return

        self._finish_factory_reset_success(payload)

    @Slot(str)
    def _on_factory_reset_runner_failure(self, message: str) -> None:
        self._factory_reset_job = None
        self._on_factory_reset_outcome(
            (False, RuntimeError(f"Фоновый исполнитель сброса: {message}"))
        )

    def _handle_factory_reset_failure(self, exc: BaseException) -> None:
        profile_restored = getattr(exc, "profile_restored", None)
        if profile_restored is False:
            log.critical(
                "Factory reset rollback was incomplete; closing unsafe application",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._factory_reset_pending = False
            self.account_view.set_factory_reset_pending(False)
            self._close_shutdown_progress()
            QMessageBox.critical(
                self,
                f"{APP_NAME} — восстановление не завершено",
                "Сброс завершился ошибкой, и локальный профиль не удалось "
                "восстановить полностью. Чтобы не продолжать работу с "
                "несогласованными данными, приложение будет закрыто.\n\n"
                f"{type(exc).__name__}: {exc}",
            )
            self._finalize_quit()
            return

        log.error(
            "Factory reset failed; restoring interactive application",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        self._abort_shutdown(
            "Не удалось удалить локальные данные. Файловый профиль "
            "восстановлен, приложение продолжает работу. Активные кампании "
            "остались остановленными и не возобновлены автоматически из "
            "соображений безопасности.\n\n"
            f"{type(exc).__name__}: {exc}",
            blockers=["factory_reset_failed"],
        )

    def _finish_factory_reset_success(self, result: object) -> None:
        if bool(getattr(result, "scheduled", False)):
            self._factory_reset_helper_pid = (
                int(getattr(result, "helper_pid", 0) or 0) or None
            )
            trace_path = getattr(result, "trace_path", "")
            log.info(
                "Factory reset helper scheduled: pid=%s trace=%s",
                self._factory_reset_helper_pid,
                trace_path,
            )
            self._set_shutdown_progress_text("Подготовка завершена. Закрытие LansetSpBot…")
            # Hide the window before leaving the event loop.  main.py will skip
            # blocking container shutdown and terminate the process immediately;
            # only then may the detached helper delete the profile.
            hide = getattr(self, "hide", None)
            if callable(hide):
                hide()

            # QApplication.quit() can remain trapped behind a Qt
            # application lifecycle callbacks even though the detached reset
            # helper has already been started.  The helper must not touch the
            # profile until this PID disappears, so the entry point
            # supplies a narrowly-scoped immediate terminator.  Windows keeps
            # the already-proven normal Qt quit path unchanged.
            terminator = getattr(self, "_factory_reset_parent_terminator", None)
            if callable(terminator):
                log.info(
                    "Factory reset helper owns the profile; terminating "
                    "parent immediately"
                )
                try:
                    terminator()
                except Exception:
                    # A custom/test terminator may fail or return unexpectedly.
                    # Fall back to the normal Qt path rather than leaving the
                    # disabled window alive.
                    log.exception(
                        "Immediate factory-reset parent termination failed; "
                        "falling back to QApplication.quit"
                    )

            QTimer.singleShot(0, self._finalize_quit)
            return

        removed_files = int(getattr(result, "removed_files", 0) or 0)
        removed_directories = int(getattr(result, "removed_directories", 0) or 0)
        self._close_shutdown_progress()
        QMessageBox.information(
            self,
            f"{APP_NAME} — сброс завершён",
            "Все локальные данные удалены. При следующем запуске программа "
            "будет работать как после первой установки.\n\n"
            f"Удалено файлов: {removed_files}; каталогов: {removed_directories}.",
        )
        self._finalize_quit()

    def _background_shutdown_blockers(self) -> list[str]:
        blockers: list[str] = []
        worker = self.queue_worker
        if worker is not None and worker.isRunning():
            state = str(getattr(worker, "lifecycle_state", "running") or "running")
            blockers.append(f"queue_worker:{state}")
        if self.account_view.is_authentication_running():
            blockers.append("telegram_auth")
        pending_calls = BackgroundCall.pending_count()
        if pending_calls:
            blockers.append(f"background_calls:{pending_calls}")
        adapter = getattr(self, "adapter", None)
        if bool(getattr(adapter, "is_secret_migration_running", lambda: False)()):
            blockers.append("local_secret_migration")
        return blockers

    def _background_threads_running(self) -> bool:
        return bool(self._background_shutdown_blockers())

    def _poll_shutdown(self) -> None:
        try:
            blockers = self._background_shutdown_blockers()
        except Exception as exc:  # noqa: BLE001 - Qt timer callback boundary
            self._shutdown_timer.stop()
            log.exception("Shutdown blocker polling failed")
            self._abort_shutdown(
                "Не удалось проверить завершение фоновых операций. "
                "Завершение отменено, приложение оставлено запущенным.\n\n"
                f"{type(exc).__name__}: {exc}",
                blockers=["shutdown_poll_failed"],
            )
            return
        if not blockers:
            self._shutdown_timer.stop()
            self._complete_shutdown()
            return
        if time.monotonic() >= self._shutdown_deadline:
            self._shutdown_timer.stop()
            self._abort_shutdown(
                "Фоновая операция не завершилась безопасно. Приложение оставлено "
                "запущенным, чтобы не повредить сессию или базу данных.",
                blockers=blockers,
            )

    def _abort_shutdown(
        self,
        message: str,
        *,
        blockers: list[str],
        critical: bool = True,
    ) -> None:
        reset_was_pending = self._factory_reset_pending
        self._factory_reset_pending = False
        self._quitting = False
        self._factory_reset_job = None
        close_progress = getattr(self, "_close_shutdown_progress", None)
        if callable(close_progress):
            close_progress()
        app = QApplication.instance()
        if app is not None:
            app.setProperty("marlen_shutdown_in_progress", False)
        try:
            self.adapter.cancel_shutdown()
        except Exception:
            log.exception("Could not restore services after aborted shutdown")
        resume_updates = getattr(self, "resume_runtime_updates", None)
        if callable(resume_updates):
            resume_updates()
        self.account_view.set_factory_reset_pending(False)
        central_widget = getattr(self, "centralWidget", None)
        central = central_widget() if callable(central_widget) else None
        if central is not None:
            central.setEnabled(True)
        self.setEnabled(True)
        self._keep_alive_timer.start()
        self.show_from_tray()
        if self._tray.isSystemTrayAvailable():
            self._tray.show()
        if critical:
            QMessageBox.critical(self, APP_NAME, message)
        else:
            QMessageBox.information(self, APP_NAME, message)
        log.critical(
            "Shutdown aborted; factory_reset=%s blockers=%s",
            reset_was_pending,
            blockers,
        )

    def _finalize_quit(self) -> None:
        """Close runtime ownership while Qt is alive, then hide and quit.

        Leaving ``app.exec()`` before the container has released its final SQLite
        ownership makes ``main.py`` perform a blocking shutdown with no running Qt
        event loop.  The tray icon disappears immediately while the last
        window remains painted but cannot process clicks.  Finalizing the already
        stopped runtime here keeps the UI responsive and makes the later ``finally``
        block a no-op on the normal coordinated shutdown path.
        """

        if bool(getattr(self, "_quit_finalize_started", False)):
            return
        self._quit_finalize_started = True

        reset_handoff = bool(getattr(self, "_factory_reset_helper_pid", None))
        finalizer = (
            None if reset_handoff else getattr(self, "_shutdown_finalizer", None)
        )
        if callable(finalizer):
            try:
                finalized = bool(finalizer())
            except Exception as exc:  # noqa: BLE001 - final GUI shutdown boundary
                log.exception("Could not finalize runtime before Qt quit")
                self._quit_finalize_started = False
                self._abort_shutdown(
                    "Не удалось завершить внутренние компоненты LansetSpBot. "
                    "Приложение оставлено открытым, чтобы не зависнуть при выходе.\n\n"
                    f"{type(exc).__name__}: {exc}",
                    blockers=["runtime_finalize_failed"],
                )
                return
            if not finalized:
                self._quit_finalize_started = False
                self._abort_shutdown(
                    "Фоновые компоненты ещё не завершились. Выход отменён; "
                    "приложение оставлено открытым.",
                    blockers=["runtime_finalize_incomplete"],
                )
                return

        self._runtime_shutdown_finalized = not reset_handoff
        self._shutdown_timer.stop()
        close_progress = getattr(self, "_close_shutdown_progress", None)
        if callable(close_progress):
            close_progress()
        suspend_updates = getattr(self, "suspend_runtime_updates", None)
        if callable(suspend_updates):
            suspend_updates()

        # Hide every LansetSpBot-owned surface before stopping the Qt event loop.
        # Otherwise the toolkit can leave a dead, unclickable snapshot of the window on
        # screen while Python executes the outer ``finally`` block.
        hide_window = getattr(self, "hide", None)
        if callable(hide_window):
            hide_window()
        tray = getattr(self, "_tray", None)
        hide_tray = getattr(tray, "hide", None)
        if callable(hide_tray):
            hide_tray()

        app = QApplication.instance()
        if app is not None:
            app.setProperty("marlen_shutdown_in_progress", True)
            self._allow_qt_quit = True
            app.quit()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        app = QApplication.instance()
        if (
            watched is app
            and event.type() == QEvent.Type.Quit
            and not self._allow_qt_quit
        ):
            self.quit_application()
            return True
        return super().eventFilter(watched, event)

    def confirm_close(self) -> bool:
        """Ask before quitting. Closing ends the process, so it must be deliberate."""

        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Закрыть LansetSpBot?\n\n"
            "Программа завершится полностью, а не свернётся. Фоновые операции "
            "останавливаются безопасно: начатая отправка доводится до конца, "
            "новые слоты не запускаются.\n\n"
            "Чтобы убрать окно с экрана, не закрывая программу, сверните его.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        """Closing the window closes the application.

        It used to hide into the tray, which left a live process holding the
        database and the Telegram session while the operator believed the
        application was closed. Repeating that accumulated several running
        copies of the program.
        """

        if self._quitting:
            # The real quit is controlled by _finalize_quit after workers stop.
            event.ignore()
            return
        event.ignore()
        if not self.confirm_close():
            return
        self.quit_application()

    def _keep_alive_tick(self) -> None:
        log.debug("Application heartbeat")


# Backward-compatible internal import alias; not shown to users.
MarlenApp = LansetSpBotApp
