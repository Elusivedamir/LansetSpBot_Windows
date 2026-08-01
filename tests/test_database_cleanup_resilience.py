from __future__ import annotations

from storage.database import Database


def test_empty_database_cleanup_preserves_nonempty_database(monkeypatch, tmp_path):
    path = tmp_path / "nonempty.db"
    original = b"already contains database data"
    path.write_bytes(original)

    database = object.__new__(Database)
    database.path = path
    forgotten_paths = []
    monkeypatch.setattr(
        "storage.database.forget_database_key",
        forgotten_paths.append,
    )

    database._discard_empty_database_file()

    assert path.read_bytes() == original
    assert forgotten_paths == []
