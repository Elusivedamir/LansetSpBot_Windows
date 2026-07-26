from __future__ import annotations

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from gui.background import BackgroundCall, connect_lifecycle_safe


class TaskWatcher(QObject):
    changed = Signal(dict)
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.task_id: int | None = None
        self._polling = False
        self._poll_job: BackgroundCall | None = None
        self._active = True
        self._generation = 0
        self._jobs: set[BackgroundCall] = set()
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.poll)

    def watch(self, task_id: int):
        self._generation += 1
        self.task_id = int(task_id)
        if self._active:
            self.timer.start()
            self.poll()

    def set_active(self, active: bool) -> None:
        """Suspend hidden-page polling without forgetting the watched task."""

        self._active = bool(active)
        if not self._active:
            self._generation += 1
            self.timer.stop()
            self._polling = False
            self._poll_job = None
            return
        if self.task_id is not None:
            self.timer.start()
            self.poll()

    def stop(self):
        self._generation += 1
        self.timer.stop()
        self.task_id = None
        self._polling = False
        self._poll_job = None

    def poll(self):
        if not self._active or self.task_id is None or self._polling:
            return
        task_id = int(self.task_id)
        generation = self._generation
        self._polling = True
        adapter = self.adapter
        job = BackgroundCall(
            lambda: adapter.get_task(task_id),
            cleanup=adapter.close_thread_connection,
        )
        self._jobs.add(job)
        self._poll_job = job

        def release(watcher: TaskWatcher) -> None:
            watcher._jobs.discard(job)
            if watcher._poll_job is job:
                watcher._poll_job = None
                watcher._polling = False

        def handle(watcher: TaskWatcher, task) -> None:
            if generation != watcher._generation or watcher.task_id != task_id:
                return
            if not task:
                watcher.stop()
                return
            watcher.changed.emit(task)
            if task.get("status") in {"completed", "failed", "cancelled"}:
                watcher.stop()
                watcher.completed.emit(task)

        def handle_error(watcher: TaskWatcher, message: str) -> None:
            if generation == watcher._generation:
                watcher.failed.emit(message)

        connect_lifecycle_safe(
            job,
            self,
            succeeded=handle,
            failed=handle_error,
            finished=release,
        )
        QThreadPool.globalInstance().start(job)
