# Run a CI subprocess with live output and an external fail-closed timeout.

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


TIMEOUT_EXIT_CODE = 124


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            kill_process_group = getattr(os, "killpg", None)
            if kill_process_group is None:
                process.kill()
            else:
                kill_process_group(
                    process.pid,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def _read_current_test(path_text: str) -> str:
    if not path_text:
        return "unknown"
    try:
        value = Path(path_text).read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return value or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--idle-timeout-seconds", type=int, default=240)
    parser.add_argument("--total-timeout-seconds", type=int, default=1800)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    current_test_path = os.environ.get("PYTEST_CURRENT_TEST_FILE", "").strip()
    timeout_path = log_path.with_name(f"{args.label}-timeout.txt")

    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    started = time.monotonic()
    last_output = [started]

    with log_path.open("w", encoding="utf-8", newline="") as log_stream:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )

        def relay() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                last_output[0] = time.monotonic()
                log_stream.write(line)
                log_stream.flush()
                sys.stdout.write(line)
                sys.stdout.flush()

        relay_thread = threading.Thread(
            target=relay,
            name=f"{args.label}-ci-output-relay",
            daemon=True,
        )
        relay_thread.start()

        timeout_reason = ""
        while process.poll() is None:
            now = time.monotonic()
            if now - last_output[0] > max(1, args.idle_timeout_seconds):
                timeout_reason = (
                    f"no subprocess output for {args.idle_timeout_seconds} seconds"
                )
                break
            if now - started > max(1, args.total_timeout_seconds):
                timeout_reason = (
                    f"total runtime exceeded {args.total_timeout_seconds} seconds"
                )
                break
            time.sleep(1)

        if timeout_reason:
            current_test = _read_current_test(current_test_path)
            message = (
                f"\n[ci-external-watchdog] {args.label} timed out: {timeout_reason}\n"
                f"[ci-external-watchdog] last pytest node: {current_test}\n"
            )
            log_stream.write(message)
            log_stream.flush()
            sys.stdout.write(message)
            sys.stdout.flush()
            timeout_path.write_text(
                f"reason={timeout_reason}\nlast_test={current_test}\n",
                encoding="utf-8",
            )
            _kill_process_tree(process)
            relay_thread.join(timeout=15)
            return TIMEOUT_EXIT_CODE

        relay_thread.join(timeout=15)
        return int(process.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
