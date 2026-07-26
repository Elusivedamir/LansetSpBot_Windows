from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Iterable

from storage.database import DatabaseError


class ImportValidationError(ValueError):
    """Raised when an import file or row does not match the supported schema."""


class ImportService:
    IMPORT_BATCH_SIZE = 1000
    MAX_CSV_BYTES = 100 * 1024 * 1024

    SUPPORTED_KINDS = frozenset({"channels", "messages", "comments"})
    REQUIRED_FIELDS = {
        "channels": ("channel_id",),
        "messages": ("channel_id", "message_id"),
        "comments": (
            "channel_id",
            "linked_chat_id",
            "post_message_id",
            "comment_message_id",
        ),
    }
    INTEGER_FIELDS = {
        "channels": ("channel_id", "linked_chat_id"),
        "messages": ("channel_id", "message_id", "author_id"),
        "comments": (
            "channel_id",
            "linked_chat_id",
            "post_message_id",
            "comment_message_id",
            "reply_to",
            "author_id",
        ),
    }

    def __init__(self, database):
        self.database = database
        self.logger = logging.getLogger("migration")

    @staticmethod
    def _clean(value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value if value != "" else None
        return value

    @classmethod
    def _to_int(cls, value: Any, field: str, row_number: int) -> int | None:
        value = cls._clean(value)
        if value is None:
            return None
        if isinstance(value, bool):
            raise ImportValidationError(f"row {row_number}: {field} must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ImportValidationError(
                f"row {row_number}: {field} must be an integer, got {value!r}"
            ) from exc

    @classmethod
    def _validate_row(
        cls, kind: str, raw: dict[str, Any], row_number: int
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ImportValidationError(f"row {row_number}: expected an object")
        row = {
            str(key).strip(): cls._clean(value)
            for key, value in raw.items()
            if key is not None
        }
        for field in cls.INTEGER_FIELDS[kind]:
            if field in row:
                row[field] = cls._to_int(row[field], field, row_number)
        missing = [
            field for field in cls.REQUIRED_FIELDS[kind] if row.get(field) is None
        ]
        if missing:
            raise ImportValidationError(
                f"row {row_number}: missing required field(s): {', '.join(missing)}"
            )
        return row

    @classmethod
    def iter_validated_rows(
        cls, kind: str, rows: Iterable[dict[str, Any]]
    ) -> Iterable[dict[str, Any]]:
        if kind not in cls.SUPPORTED_KINDS:
            raise ImportValidationError(f"unsupported import kind: {kind!r}")
        for row_number, raw in enumerate(rows, start=2):
            yield cls._validate_row(kind, raw, row_number)

    @classmethod
    def validate_rows(
        cls, kind: str, rows: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return list(cls.iter_validated_rows(kind, rows))

    def _csv_rows(self, path: str | Path) -> Iterable[dict[str, Any]]:
        csv_path = Path(path).expanduser()
        if not csv_path.is_file():
            raise ImportValidationError(f"CSV file does not exist: {csv_path}")
        if csv_path.stat().st_size > self.MAX_CSV_BYTES:
            limit_mb = self.MAX_CSV_BYTES // (1024 * 1024)
            raise ImportValidationError(
                f"CSV is larger than the supported {limit_mb} MB limit: {csv_path}"
            )
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames:
                    raise ImportValidationError(f"CSV has no header: {csv_path}")
                yield from reader
        except UnicodeDecodeError as exc:
            raise ImportValidationError(
                f"CSV must be UTF-8 encoded: {csv_path}"
            ) from exc
        except csv.Error as exc:
            raise ImportValidationError(f"Invalid CSV {csv_path}: {exc}") from exc

    def import_csv(self, path: str | Path) -> list[dict[str, Any]]:
        """Compatibility helper for callers that explicitly need parsed rows."""
        return list(self._csv_rows(path))

    def import_file(self, kind: str, path: str | Path) -> int:
        try:
            rows = self.iter_validated_rows(kind, self._csv_rows(path))
            return int(
                self.database.import_rows(kind, rows, batch_size=self.IMPORT_BATCH_SIZE)
            )
        except (ImportValidationError, DatabaseError):
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to import {kind}: {exc}") from exc

    def migrate(self, files: dict[str, str | Path]) -> dict[str, Any]:
        if not isinstance(files, dict) or not files:
            raise ImportValidationError("files must be a non-empty object")
        report: dict[str, Any] = {
            "channels": 0,
            "messages": 0,
            "comments": 0,
            "errors": [],
        }
        for kind, path in files.items():
            try:
                report[kind] = self.import_file(kind, path)
            except Exception as exc:
                self.logger.exception("migration error for %s", kind)
                report["errors"].append(
                    {"kind": kind, "path": str(path), "error": str(exc)}
                )
        return report
