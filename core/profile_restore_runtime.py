"""Detached profile-restore execution and startup result handoff."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import Config
from core.factory_reset_runtime import (
    _detached_popen_kwargs,
    _entrypoint_command,
    _relaunch_application,
    _remove_stale_instance_locks,
    _wait_for_parent_exit,
)
from core.local_security import harden_private_file
from core.private_trace import helper_trace_path, open_helper_trace
from core.profile_backup import (
    ProfileRestoreResult,
    recover_incomplete_profile_restore as recover_restore_transaction,
    restore_profile_backup,
)
from core.secret_store import SecretStore

PROFILE_RESTORE_HELPER_FLAG = "--profile-restore-helper"
PROFILE_RESTORE_NO_RELAUNCH_FLAG = "--profile-restore-no-relaunch"
PROFILE_RESTORE_RESULT_NAME = ".profile-restore-result.json"
PROFILE_RESTORE_RESULT_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class ScheduledProfileRestore:
    helper_pid: int
    trace_path: Path
    archive_path: Path
    scheduled: bool = True


def _trace_path(parent_pid: int) -> Path:
    return helper_trace_path("profile-restore", parent_pid)


def launch_detached_profile_restore(
    archive_path: Path, *, parent_pid: int | None = None
) -> ScheduledProfileRestore:
    """Start a helper that activates a validated backup after this process exits."""

    parent_pid = int(parent_pid or os.getpid())
    archive_path = Path(archive_path).expanduser().resolve()
    trace_path = _trace_path(parent_pid)
    command = [
        *_entrypoint_command(),
        PROFILE_RESTORE_HELPER_FLAG,
        str(parent_pid),
        str(archive_path),
    ]
    environment = os.environ.copy()
    environment.pop("MARLEN_PROFILE_RESTORE_TEST_NO_RELAUNCH", None)
    with open_helper_trace(trace_path) as trace:
        process = subprocess.Popen(  # noqa: S603 - command is fully internal
            command,
            stdout=trace,
            stderr=subprocess.STDOUT,
            env=environment,
            **_detached_popen_kwargs(),
        )
    return ScheduledProfileRestore(
        helper_pid=int(process.pid),
        trace_path=trace_path,
        archive_path=archive_path,
    )


def _result_path(config: Config) -> Path:
    return config.paths.root / PROFILE_RESTORE_RESULT_NAME


def _write_result(config: Config, payload: dict[str, Any]) -> None:
    config.paths.ensure()
    destination = _result_path(config)
    temporary = destination.with_name(f"{destination.name}.{os.getpid()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > PROFILE_RESTORE_RESULT_MAX_BYTES:
        raise RuntimeError("Profile restore result is unexpectedly large")
    temporary.write_bytes(encoded)
    if not harden_private_file(temporary):
        temporary.unlink(missing_ok=True)
        raise PermissionError(f"Could not protect restore result {temporary}")
    os.replace(temporary, destination)
    if not harden_private_file(destination):
        destination.unlink(missing_ok=True)
        raise PermissionError(f"Could not protect restore result {destination}")


def consume_profile_restore_result(config: Config) -> dict[str, Any] | None:
    path = _result_path(config)
    try:
        exists = path.exists() or path.is_symlink()
    except OSError:
        return None
    if not exists or path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > PROFILE_RESTORE_RESULT_MAX_BYTES:
            return {"ok": False, "message": "Файл результата restore повреждён"}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"ok": False, "message": "Некорректный результат restore"}
        return payload
    except Exception as exc:  # noqa: BLE001 - startup remains recoverable
        return {
            "ok": False,
            "message": f"Не удалось прочитать результат восстановления: {exc}",
        }
    finally:
        path.unlink(missing_ok=True)


def recover_incomplete_profile_restore(config: Config) -> dict[str, Any] | None:
    restored = recover_restore_transaction(config.paths)
    if not restored:
        return None
    return {
        "ok": False,
        "profile_restored": True,
        "message": (
            "Обнаружено незавершённое восстановление профиля. Исходный рабочий "
            "профиль автоматически возвращён; резервная копия не активирована."
        ),
    }


def _result_payload(
    result: ProfileRestoreResult, *, trace_path: Path
) -> dict[str, Any]:
    previous = result.previous_database_backup
    return {
        "ok": True,
        "message": "Профиль LansetSpBot успешно восстановлен из резервной копии.",
        "schema_version": int(result.schema_version),
        "file_count": int(result.file_count),
        "contained_sessions": bool(result.contained_sessions),
        "previous_database_backup": str(previous) if previous else "",
        "trace_path": str(trace_path),
    }


def run_profile_restore_helper(
    parent_pid: int, archive_path: Path, *, relaunch: bool = True
) -> int:
    """Wait for the GUI, restore the profile transactionally, then relaunch."""

    config = Config()
    trace_path = _trace_path(int(parent_pid))
    try:
        print(f"PROFILE_RESTORE_WAIT_PARENT pid={int(parent_pid)}", flush=True)
        _wait_for_parent_exit(int(parent_pid))
        print("PROFILE_RESTORE_PARENT_EXITED", flush=True)
        result = restore_profile_backup(
            archive_path=Path(archive_path),
            paths=config.paths,
            secret_path=SecretStore().fallback_path,
        )
        _remove_stale_instance_locks(config)
        _write_result(config, _result_payload(result, trace_path=trace_path))
        print("PROFILE_RESTORE_RESULT_WRITTEN ok=1", flush=True)
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - helper persists a user-visible result
        profile_restored = getattr(exc, "profile_restored", None)
        try:
            _write_result(
                config,
                {
                    "ok": False,
                    "profile_restored": profile_restored,
                    "trace_path": str(trace_path),
                    "message": (
                        "Восстановление профиля завершилось ошибкой. Исходный "
                        "профиль возвращён, если это было возможно.\n\n"
                        f"{type(exc).__name__}: {exc}\n\n"
                        f"Технический журнал: {trace_path}"
                    ),
                },
            )
        except Exception as result_exc:  # noqa: BLE001
            print(
                "PROFILE_RESTORE_RESULT_WRITE_FAILED "
                f"{type(result_exc).__name__}: {result_exc}",
                flush=True,
            )
        print(f"PROFILE_RESTORE_FAILED {type(exc).__name__}: {exc}", flush=True)
        exit_code = 1

    if relaunch:
        try:
            _relaunch_application()
        except Exception as exc:  # noqa: BLE001
            print(
                f"PROFILE_RESTORE_RELAUNCH_FAILED {type(exc).__name__}: {exc}",
                flush=True,
            )
            return 2 if exit_code == 0 else exit_code
    return exit_code
