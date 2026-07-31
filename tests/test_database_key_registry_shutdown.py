from __future__ import annotations

from pathlib import Path

from storage.database import Database
import storage.database as database_module


def test_database_final_shutdown_forgets_key_and_can_reopen(
    monkeypatch, tmp_path: Path
) -> None:
    database = Database(tmp_path / "shutdown-key.db")
    database.set_setting("probe", "value")
    original_key = database._database_key
    forgotten: list[Path] = []
    real_forget = database_module.forget_database_key

    def record_forget(path: str | Path) -> None:
        forgotten.append(Path(path))
        real_forget(path)

    monkeypatch.setattr(database_module, "forget_database_key", record_forget)

    database.finalize_shutdown()

    assert forgotten == [database.path]
    assert database._database_key is None
    assert original_key is not None
    # A post-finalization access must not reuse the dropped object reference. The
    # driver re-derives the DPAPI-bound key (or the explicit pytest test key) and
    # transparently opens a fresh thread-local connection.
    assert database.get_setting("probe") == "value"
    database.finalize_shutdown()


def test_application_container_uses_final_database_shutdown_contract() -> None:
    source = Path("core/composition.py").read_text(encoding="utf-8")

    assert source.count("self.database.finalize_shutdown()") == 2
    assert "self.database.close_thread_connection()\n        return True" not in source
