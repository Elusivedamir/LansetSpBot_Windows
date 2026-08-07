from __future__ import annotations

import json
import types
from pathlib import Path


import core.factory_reset_runtime as runtime
from core.factory_reset import FactoryResetError


def test_entrypoint_command_uses_main_script_when_not_frozen(monkeypatch) -> None:
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    command = runtime._entrypoint_command()
    assert command[0] == runtime.sys.executable
    assert Path(command[1]).name == "main.py"


def test_detached_popen_kwargs_are_platform_specific(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="posix"))
    posix = runtime._detached_popen_kwargs()
    assert posix["start_new_session"] is True
    assert "creationflags" not in posix

    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="nt"))
    windows = runtime._detached_popen_kwargs()
    assert "creationflags" in windows
    assert "start_new_session" not in windows


def test_pid_exists_maps_os_errors(monkeypatch) -> None:
    assert runtime._pid_exists(0) is False

    monkeypatch.setattr(
        runtime, "os", types.SimpleNamespace(kill=lambda _pid, _sig: None)
    )
    assert runtime._pid_exists(123) is True

    def missing(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(kill=missing))
    assert runtime._pid_exists(123) is False

    def denied(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(kill=denied))
    assert runtime._pid_exists(123) is True

    def generic(_pid: int, _sig: int) -> None:
        raise OSError("bad pid")

    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(kill=generic))
    assert runtime._pid_exists(123) is False


def test_transient_windows_reset_error_is_strict(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="nt"))
    assert runtime._is_transient_windows_reset_error(
        PermissionError("WinError 32 file is being used by another process")
    )
    assert not runtime._is_transient_windows_reset_error(RuntimeError("other"))

    irreversible = FactoryResetError("failed", profile_restored=False)
    assert not runtime._is_transient_windows_reset_error(irreversible)

    monkeypatch.setattr(runtime, "os", types.SimpleNamespace(name="posix"))
    assert not runtime._is_transient_windows_reset_error(
        PermissionError("WinError 32")
    )


class _ResultPath:
    def __init__(self, text: str, *, unlink_error: BaseException | None = None) -> None:
        self.text = text
        self.unlink_error = unlink_error
        self.unlink_calls = 0

    def exists(self) -> bool:
        return True

    def is_symlink(self) -> bool:
        return False

    def is_file(self) -> bool:
        return True

    def stat(self):
        return types.SimpleNamespace(st_size=len(self.text.encode("utf-8")))

    def read_text(self, *, encoding: str) -> str:
        assert encoding == "utf-8"
        return self.text

    def unlink(self, *, missing_ok: bool) -> None:
        assert missing_ok is True
        self.unlink_calls += 1
        if self.unlink_error is not None:
            raise self.unlink_error


def test_consume_factory_reset_result_does_not_crash_if_cleanup_fails(
    monkeypatch,
) -> None:
    path = _ResultPath(
        json.dumps({"ok": True, "message": "done"}),
        unlink_error=PermissionError("locked"),
    )
    monkeypatch.setattr(runtime, "_result_path", lambda _config: path)

    result = runtime.consume_factory_reset_result(object())

    assert result == {"ok": True, "message": "done"}
    assert path.unlink_calls == 1


def test_consume_factory_reset_result_rejects_invalid_payload(monkeypatch) -> None:
    path = _ResultPath("[]")
    monkeypatch.setattr(runtime, "_result_path", lambda _config: path)
    assert runtime.consume_factory_reset_result(object()) == {
        "ok": False,
        "message": "Некорректный результат сброса",
    }

    broken = _ResultPath("{")
    monkeypatch.setattr(runtime, "_result_path", lambda _config: broken)
    result = runtime.consume_factory_reset_result(object())
    assert result is not None
    assert result["ok"] is False
    assert "Не удалось прочитать" in str(result["message"])


def test_remove_stale_instance_locks_ignores_race(tmp_path: Path) -> None:
    root = tmp_path
    first = root / ".instance-a.lock"
    second = root / ".instance-b.lock"
    first.write_text("x", encoding="utf-8")
    second.write_text("x", encoding="utf-8")

    config = types.SimpleNamespace(paths=types.SimpleNamespace(root=root))
    runtime._remove_stale_instance_locks(config)

    assert list(root.glob(".instance-*.lock")) == []
