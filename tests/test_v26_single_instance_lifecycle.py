from __future__ import annotations
import uuid
import shiboken6
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication
from core.single_instance import SingleInstance


def test_release_notification_socket_tolerates_deleted_cpp_object():
    _app = QApplication.instance() or QApplication([])
    instance = SingleInstance(name="v26." + uuid.uuid4().hex)
    sock = QLocalSocket(instance)
    instance._notification_sockets.add(sock)
    shiboken6.delete(sock)
    instance._release_notification_socket(sock)
    assert sock not in instance._notification_sockets
