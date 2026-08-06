"""Detached factory-reset execution and startup result handoff.

The live GUI process must never delete its own SQLite database, Telethon session,
or log directory.  Even with timers stopped, Qt can still dispatch queued callbacks
and Python/SQLite can retain thread-owned handles until process teardown.  The
reset is therefore delegated to a fresh process that waits for the GUI process to
exit before touching any local profile file.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import Config
from core.factory_reset import (
    FactoryResetError,
    FactoryResetResult,
    recover_incomplete_factory_reset as recover_reset_transaction,
    reset_local_state,
)
from core.local_security import harden_private_file
from core.private_trace import helper_trace_path, open_helper_trace
from core.secret_store import SecretStore
from storage.database import Database

FACTORY_RESET_HELPER_FLAG = "--factory-reset-helper"
FACTORY_RESET_NO_RELAUNCH_FLAG = "--factory-reset-no-relaunch"
FACTORY_RESET_RESULT_NAME = ".factory-reset-result.json"
FACTORY_RESET_RESULT_MAX_BYTES = 64 * 1024
PARENT_EXIT_TIMEOUT_SECONDS = 180.0

REQUIRED_PROFILE_TABLES = frozenset(
    {
        "account_activity_leases",
        "channels",
        "comment_campaigns",
        "comment_deliveries",
        "comment_history",
        "comment_limits",
        "comment_schedule",
        "comment_templates",
        "comments",
        "direct_message_deliveries",
        "join_campaigns",
        "join_events",
        "join_schedule",
        "logs",
        "messages",
        "migrations",
        "saved_dialog_memberships",
        "saved_dialogs",
        "settings",
        "tasks",
    }
)


@dataclass(frozen=True)
class ScheduledFactoryReset:
    """Result returned to the GUI after the detached helper was started."""

    helper_pid: int
    trace_path: Path
    scheduled: bool = True
    removed_files: int = 0
    removed_directories: int = 0


def _entrypoint_command() -> list[str]:
    """Return a command that starts this Marlen build in a new process."""

    if bool(getattr(sys, "frozen", False)):
        return [sys.executable]
    main_script = Path(__file__).resolve().parents[1] / "main.py"
    return [sys.executable, str(main_script)]


def _detached_popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _trace_path(parent_pid: int) -> Path:
    return helper_trace_path("factory-reset", parent_pid)


def launch_detached_factory_reset(
    *, parent_pid: int | None = None
) -> ScheduledFactoryReset:
    """Start a helper that waits for this process to exit, then resets the profile."""

    parent_pid = int(parent_pid or os.getpid())
    trace_path = _trace_path(parent_pid)
    command = [
        *_entrypoint_command(),
        FACTORY_RESET_HELPER_FLAG,
        str(parent_pid),
    ]
    environment = os.environ.copy()
    environment.pop("MARLEN_FACTORY_RESET_TEST_NO_RELAUNCH", None)
    with open_helper_trace(trace_path) as trace:
        process = subprocess.Popen(  # noqa: S603 - command is fully internal
            command,
            stdout=trace,
            stderr=subprocess.STDOUT,
            env=environment,
            **_detached_popen_kwargs(),
        )
    return ScheduledFactoryReset(
        helper_pid=int(process.pid),
        trace_path=trace_path,
    )


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_parent_exit_windows(parent_pid: int) -> None:
    """Wait for one Windows PID without using ``os.kill(pid, 0)``.

    ``os.kill`` has platform-specific signal semantics on Windows and is not a
    reliable process-liveness primitive.  Waiting on a process handle is both
    race-free and guarantees that all handles owned by the GUI process have
    been released before SQLite/session deletion begins.
    """

    synchronize = 0x00100000
    query_limited_information = 0x1000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_invalid_parameter = 87

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise RuntimeError("Windows process API is unavailable")
    get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
    kernel32 = win_dll("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    wait_for_single_object.restype = ctypes.c_ulong
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = open_process(
        synchronize | query_limited_information,
        0,
        int(parent_pid),
    )
    if not handle:
        error = int(get_last_error())
        if error == error_invalid_parameter:
            return
        raise OSError(error, f"OpenProcess({parent_pid}) failed")

    try:
        timeout_ms = max(1, int(PARENT_EXIT_TIMEOUT_SECONDS * 1000))
        result = int(wait_for_single_object(handle, timeout_ms))
        if result == wait_object_0:
            return
        if result == wait_timeout:
            raise TimeoutError(
                "Основной процесс LansetSpBot не завершился за отведённое время; "
                "локальные данные не удалены"
            )
        error = int(get_last_error())
        raise OSError(error, f"WaitForSingleObject({parent_pid}) failed: {result}")
    finally:
        close_handle(handle)


def _wait_for_parent_exit(parent_pid: int) -> None:
    if os.name == "nt":
        _wait_for_parent_exit_windows(parent_pid)
    else:
        deadline = time.monotonic() + PARENT_EXIT_TIMEOUT_SECONDS
        while _pid_exists(parent_pid):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Основной процесс LansetSpBot не завершился за отведённое время; "
                    "локальные данные не удалены"
                )
            time.sleep(0.1)
    # Windows may briefly retain antivirus/indexer handles after process exit.
    # Give SQLite, logging and Telethon files a bounded release window.
    time.sleep(0.75 if os.name == "nt" else 0.25)


def initialize_empty_profile(config: Config) -> None:
    """Create and verify every directory/table required by a clean startup."""

    config.paths.ensure()
    database = Database(config.database_path, busy_timeout_ms=1_000)
    try:
        with database.get_connection() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {str(row[0]) for row in rows}
            missing = sorted(REQUIRED_PROFILE_TABLES - table_names)
            if missing:
                raise RuntimeError(
                    "после сброса отсутствуют таблицы SQLite: " + ", ".join(missing)
                )
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != database.SCHEMA_VERSION:
                raise RuntimeError(
                    "после сброса создана неверная версия схемы SQLite: "
                    f"{version}, ожидалась {database.SCHEMA_VERSION}"
                )
            integrity = str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            ).strip()
            if integrity.lower() != "ok":
                raise RuntimeError(
                    f"проверка целостности новой SQLite-базы: {integrity}"
                )
    finally:
        database.close_thread_connection()


def recover_incomplete_factory_reset(config: Config) -> dict[str, Any] | None:
    """Resolve a hard-crashed reset before ApplicationContainer opens SQLite."""

    restored = recover_reset_transaction(
        database_path=config.database_path,
        paths=config.paths,
        secret_path=SecretStore().fallback_path,
    )
    if not restored:
        return None
    return {
        "ok": False,
        "profile_restored": True,
        "message": (
            "Обнаружен незавершённый заводской сброс после аварийного "
            "завершения. Исходный локальный профиль автоматически восстановлен; "
            "сброс не был применён."
        ),
    }


def _result_path(config: Config) -> Path:
    return config.paths.root / FACTORY_RESET_RESULT_NAME


def _write_result(config: Config, payload: dict[str, Any]) -> None:
    config.paths.ensure()
    destination = _result_path(config)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > FACTORY_RESET_RESULT_MAX_BYTES:
        raise RuntimeError("Factory reset result is unexpectedly large")
    temporary.write_bytes(encoded)
    if not harden_private_file(temporary):
        temporary.unlink(missing_ok=True)
        raise PermissionError(f"Could not protect factory-reset result {temporary}")
    os.replace(temporary, destination)
    if not harden_private_file(destination):
        destination.unlink(missing_ok=True)
        raise PermissionError(f"Could not protect factory-reset result {destination}")


def consume_factory_reset_result(config: Config) -> dict[str, Any] | None:
    """Read and remove the one-shot result left by the detached helper."""

    path = _result_path(config)
    try:
        exists = path.exists() or path.is_symlink()
    except OSError:
        return None
    if not exists or path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > FACTORY_RESET_RESULT_MAX_BYTES:
            return {"ok": False, "message": "Файл результата сброса повреждён"}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"ok": False, "message": "Некорректный результат сброса"}
        return payload
    except Exception as exc:  # noqa: BLE001 - startup must remain recoverable
        return {"ok": False, "message": f"Не удалось прочитать результат сброса: {exc}"}
    finally:
        path.unlink(missing_ok=True)


def _remove_stale_instance_locks(config: Config) -> None:
    for candidate in config.paths.root.glob(".instance-*.lock"):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _relaunch_application() -> None:
    subprocess.Popen(  # noqa: S603 - command is fully internal
        _entrypoint_command(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        **_detached_popen_kwargs(),
    )


def _is_transient_windows_reset_error(exc: BaseException) -> bool:
    if os.name != "nt":
        return False
    if isinstance(exc, FactoryResetError) and exc.profile_restored is False:
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "winerror 5",
        "winerror 32",
        "winerror 33",
        "permissionerror",
        "access is denied",
        "being used by another process",
        "процесс не может получить доступ",
        "отказано в доступе",
    )
    return any(marker in text for marker in markers)


def _reset_with_windows_lock_retries(config: Config) -> FactoryResetResult:
    delays = (0.0, 0.75, 1.5, 3.0) if os.name == "nt" else (0.0,)
    last_error: BaseException | None = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            print(
                f"FACTORY_RESET_RETRY attempt={attempt} delay={delay:.2f}s",
                flush=True,
            )
            time.sleep(delay)
        try:
            return reset_local_state(
                database_path=config.database_path,
                paths=config.paths,
                secret_path=SecretStore().fallback_path,
                post_reset_initializer=lambda: initialize_empty_profile(config),
            )
        except Exception as exc:  # noqa: BLE001 - retry only proven lock failures
            last_error = exc
            if attempt >= len(delays) or not _is_transient_windows_reset_error(exc):
                raise
    if last_error is None:
        raise RuntimeError(
            "Factory reset retry loop ended without a result and without an error"
        )
    raise last_error


def run_factory_reset_helper(parent_pid: int, *, relaunch: bool = True) -> int:
    """Wait for the GUI process, reset local state, record outcome, and relaunch."""

    config = Config()
    trace_path = _trace_path(int(parent_pid))
    try:
        print(f"FACTORY_RESET_WAIT_PARENT pid={int(parent_pid)}", flush=True)
        _wait_for_parent_exit(int(parent_pid))
        print("FACTORY_RESET_PARENT_EXITED", flush=True)
        result = _reset_with_windows_lock_retries(config)
        print("FACTORY_RESET_PROFILE_REBUILT", flush=True)
        _remove_stale_instance_locks(config)
        _write_result(
            config,
            {
                "ok": True,
                "message": "Заводской сброс завершён. Создана новая пустая база данных.",
                "removed_files": int(result.removed_files),
                "removed_directories": int(result.removed_directories),
                "trace_path": str(trace_path),
            },
        )
        print("FACTORY_RESET_RESULT_WRITTEN ok=1", flush=True)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - helper must persist a user-visible result
        profile_restored = getattr(exc, "profile_restored", None)
        try:
            _write_result(
                config,
                {
                    "ok": False,
                    "profile_restored": profile_restored,
                    "trace_path": str(trace_path),
                    "message": (
                        "Заводской сброс завершился ошибкой. Исходный профиль "
                        "восстановлен, если это было возможно.\n\n"
                        f"{type(exc).__name__}: {exc}\n\n"
                        f"Технический журнал: {trace_path}"
                    ),
                },
            )
        except Exception as result_exc:  # noqa: BLE001 - preserve both failures in trace
            print(
                "FACTORY_RESET_RESULT_WRITE_FAILED "
                f"{type(result_exc).__name__}: {result_exc}",
                flush=True,
            )
        print(f"FACTORY_RESET_FAILED {type(exc).__name__}: {exc}", flush=True)
        exit_code = 1

    if relaunch:
        try:
            print(f"FACTORY_RESET_RELAUNCH exit_code={exit_code}", flush=True)
            _relaunch_application()
        except Exception as exc:  # noqa: BLE001 - result remains available next launch
            print(
                f"FACTORY_RESET_RELAUNCH_FAILED {type(exc).__name__}: {exc}", flush=True
            )
            return 2 if exit_code == 0 else exit_code
    return exit_code
