from __future__ import annotations

from unittest.mock import MagicMock

from storage.database import Database


def _database_without_initialization(path):
    database = Database.__new__(Database)
    database.path = path
    database.close_thread_connection = MagicMock()
    return database


def test_discard_empty_database_file_ignores_directory(tmp_path) -> None:
    database_path = tmp_path / "marlen.db"
    database_path.mkdir()

    database = _database_without_initialization(database_path)
    database._discard_empty_database_file()

    assert database_path.is_dir()
    database.close_thread_connection.assert_not_called()


def test_discard_empty_database_file_keeps_nonempty_database(tmp_path) -> None:
    database_path = tmp_path / "marlen.db"
    database_path.write_bytes(b"not-empty")

    database = _database_without_initialization(database_path)
    database._discard_empty_database_file()

    assert database_path.read_bytes() == b"not-empty"
    database.close_thread_connection.assert_not_called()


def test_missing_file_is_detected_through_exception_cause() -> None:
    missing = FileNotFoundError("SQLite sidecar disappeared")
    wrapper = RuntimeError("artifact validation failed")
    wrapper.__cause__ = missing

    assert Database._caused_by_missing_file(wrapper) is True


def test_unrelated_exception_is_not_treated_as_missing_file() -> None:
    wrapper = RuntimeError("permission denied")
    wrapper.__cause__ = ValueError("invalid artifact")

    assert Database._caused_by_missing_file(wrapper) is False
