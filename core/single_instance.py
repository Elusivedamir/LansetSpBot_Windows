"""Single-process guard based on Qt local sockets."""

from __future__ import annotations

import hashlib

import shiboken6
from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from core.paths import APP_PATHS


class SingleInstance(QObject):
    """Own a local server and forward activation to the primary process."""

    activation_requested = Signal()

    def __init__(self, name: str | None = None) -> None:
        super().__init__()
        if name is None:
            profile = hashlib.sha256(str(APP_PATHS.root).encode("utf-8")).hexdigest()[
                :12
            ]
            name = f"com.marlen.pro.{profile}"
        self.name = name
        APP_PATHS.ensure()
        lock_name = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        self._lock = QLockFile(str(APP_PATHS.root / f".instance-{lock_name}.lock"))
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._drain_connections)
        self._notification_sockets: set[QLocalSocket] = set()
        self._acquired = False

    def acquire(self) -> bool:
        # QLockFile is the atomic ownership primitive. A socket-only probe has a
        # startup race: two processes can both observe no server and then one can
        # remove the other's freshly-created socket with removeServer().
        if not self._lock.tryLock(0):
            self._notify_primary()
            return False

        # Only the lock owner may remove a stale socket left by a crashed process.
        QLocalServer.removeServer(self.name)
        self._acquired = self.server.listen(self.name)
        if not self._acquired:
            self._lock.unlock()
        return self._acquired

    def _notify_primary(self) -> None:
        # Keep the client socket alive until the primary process consumes the
        # activation payload. Destroying a local QLocalSocket immediately after
        # write() can discard the named-pipe message on Windows before the
        # primary event loop gets a chance to accept the connection.
        probe = QLocalSocket(self)
        self._notification_sockets.add(probe)
        probe.disconnected.connect(
            lambda socket=probe: self._release_notification_socket(socket)
        )
        probe.connectToServer(self.name)
        if not probe.waitForConnected(800):
            self._release_notification_socket(probe)
            return
        if probe.write(b"activate") < 0:
            self._release_notification_socket(probe)
            return
        probe.flush()
        probe.waitForBytesWritten(800)
        # The primary closes its side after reading. Do not disconnect here:
        # keeping the pipe open is what makes delivery reliable on Windows.

    def _release_notification_socket(self, socket: QLocalSocket) -> None:
        if socket not in self._notification_sockets:
            return
        self._notification_sockets.discard(socket)
        # The disconnected signal may be delivered while QObject parent teardown
        # has already destroyed the C++ QLocalSocket.  The Python wrapper can
        # still exist, but every Qt method then raises RuntimeError.
        if not shiboken6.isValid(socket):
            return
        if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            socket.abort()
        socket.deleteLater()

    def close(self) -> None:
        for socket in tuple(self._notification_sockets):
            self._release_notification_socket(socket)
        if not self._acquired:
            return
        self._acquired = False
        if self.server.isListening():
            self.server.close()
        QLocalServer.removeServer(self.name)
        self._lock.unlock()

    def _drain_connections(self) -> None:
        requested = False
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                continue
            socket.waitForReadyRead(100)
            if bytes(socket.readAll().data()).startswith(b"activate"):
                requested = True
            socket.disconnectFromServer()
            socket.deleteLater()
        if requested:
            self.activation_requested.emit()
