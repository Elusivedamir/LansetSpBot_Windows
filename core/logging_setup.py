"""Unified Marlen logging setup with a bounded on-disk footprint."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.local_security import (
    LocalFileSecurityError,
    harden_private_file,
    validate_private_regular_file,
)
from core.paths import APP_PATHS
from core.redaction import sanitize_log_text

# One shareable technical log. It stays as one physical file so an
# operator can attach it after a Windows test without hunting down rotations.
# When the cap is reached the oldest bytes are discarded in-place; no .1 file
# is created.
FILE_LOG_SEGMENT_BYTES = 16 * 1024 * 1024
FILE_LOG_RETAIN_BYTES = 12 * 1024 * 1024
FILE_LOG_BACKUP_COUNT = 0
FILE_LOG_TOTAL_BYTES = FILE_LOG_SEGMENT_BYTES
FILE_LOG_RECORD_BYTES = 64 * 1024


class _BoundedFormatter(logging.Formatter):
    """Bound one fully formatted record without mutating the shared LogRecord."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = sanitize_log_text(super().format(record))
        encoded = rendered.encode("utf-8", errors="replace")
        if len(encoded) <= FILE_LOG_RECORD_BYTES:
            return rendered
        marker = b"\n... [log record truncated to 64 KiB] ...\n"
        available = FILE_LOG_RECORD_BYTES - len(marker)
        head_size = available // 2
        tail_size = available - head_size
        head = encoded[:head_size].decode("utf-8", errors="ignore")
        tail = encoded[-tail_size:].decode("utf-8", errors="ignore")
        return f"{head}{marker.decode('ascii')}{tail}"


class _Utf8RotatingFileHandler(RotatingFileHandler):
    """Rotate against encoded bytes so every segment honors the hard cap."""

    def _open(self):
        stream = super()._open()
        if not harden_private_file(Path(self.baseFilename)):
            stream.close()
            raise PermissionError(
                f"Could not restrict private log file {self.baseFilename}"
            )
        return stream

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        if self.maxBytes <= 0:
            return False
        if self.stream is None:
            self.stream = self._open()
        rendered = f"{self.format(record)}{self.terminator}".encode(
            self.encoding or "utf-8", errors=self.errors or "strict"
        )
        self.stream.seek(0, 2)
        return self.stream.tell() + len(rendered) > self.maxBytes

    def doRollover(self) -> None:  # noqa: N802 - logging API
        # RotatingFileHandler with backupCount=0 only reopens in append mode.
        # Truncate oldest bytes in-place so marlen.log remains a single file.
        if self.stream is not None:
            self.stream.flush()
            self.stream.close()
            self.stream = None
        _truncate_to_tail(Path(self.baseFilename), FILE_LOG_RETAIN_BYTES)
        if not self.delay:
            self.stream = self._open()


def _truncate_to_tail(path: Path, max_bytes: int) -> None:
    """Keep only the newest bytes of one existing log file."""

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size <= max_bytes:
        return

    with path.open("rb") as source:
        source.seek(-max_bytes, 2)
        data = source.read(max_bytes)
    # Avoid starting the retained UTF-8 text in the middle of a continuation
    # byte. The rest of the file remains byte-for-byte unchanged.
    while data and data[0] & 0xC0 == 0x80:
        data = data[1:]
    path.write_bytes(data)


def _validate_existing_log_file(path: Path) -> None:
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:
        raise RuntimeError(f"Could not inspect log file {path}: {exc}") from exc
    if not exists:
        return
    try:
        validate_private_regular_file(path)
    except LocalFileSecurityError as exc:
        raise RuntimeError(f"Unsafe local log file: {exc}") from exc


def _enforce_existing_file_budget(log_file: Path) -> None:
    """Remove obsolete rotations and cap files left by older releases."""

    _validate_existing_log_file(log_file)
    for candidate in log_file.parent.glob(f"{log_file.name}.*"):
        suffix = candidate.name.removeprefix(f"{log_file.name}.")
        try:
            index = int(suffix)
        except ValueError:
            continue
        _validate_existing_log_file(candidate)
        if index > FILE_LOG_BACKUP_COUNT:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    _truncate_to_tail(log_file, FILE_LOG_RETAIN_BYTES)
    for index in range(1, FILE_LOG_BACKUP_COUNT + 1):
        _truncate_to_tail(
            log_file.with_name(f"{log_file.name}.{index}"),
            FILE_LOG_SEGMENT_BYTES,
        )


def setup_logging() -> logging.Logger:
    APP_PATHS.ensure()
    log_file = APP_PATHS.logs / "marlen.log"
    _enforce_existing_file_budget(log_file)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    matching_handlers = [
        handler
        for handler in tuple(root.handlers)
        if isinstance(handler, RotatingFileHandler)
        and getattr(handler, "baseFilename", "") == str(log_file)
    ]
    # ``logging.shutdown()`` closes streams but intentionally leaves handler
    # objects attached to the root logger. During factory reset the log
    # directory may then be removed and recreated, so stale handlers can still
    # point at unlinked inodes. Remove every matching handler before reopening
    # the current file; this also repairs accidental duplicates from an older
    # process setup path.
    for handler in matching_handlers:
        root.removeHandler(handler)
        handler.close()

    file_handler = _Utf8RotatingFileHandler(
        log_file,
        maxBytes=FILE_LOG_SEGMENT_BYTES,
        backupCount=FILE_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    root.addHandler(file_handler)

    if not harden_private_file(log_file):
        root.removeHandler(file_handler)
        file_handler.close()
        raise RuntimeError(f"Could not restrict private log file {log_file}")
    file_handler.setFormatter(
        _BoundedFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    session_logger = logging.getLogger("marlen")
    session_logger.info("===== LansetSpBot logging session started =====")
    return session_logger


def mirror_activity_log(level: object, message: object, *, account_id: int = 0) -> None:
    """Mirror the persistent GUI journal into the same shareable text log."""

    root = logging.getLogger()
    active = any(
        isinstance(handler, _Utf8RotatingFileHandler)
        and Path(str(getattr(handler, "baseFilename", ""))).name == "marlen.log"
        for handler in tuple(root.handlers)
    )
    if not active:
        # Database-only tests intentionally do not configure file logging.
        return
    normalized = str(level or "INFO").strip().upper()
    level_number = getattr(logging, normalized, logging.INFO)
    if not isinstance(level_number, int):
        level_number = logging.INFO
    logging.getLogger("marlen.activity").log(
        level_number,
        "[Живой журнал][account=%s] %s",
        int(account_id or 0),
        message,
    )
