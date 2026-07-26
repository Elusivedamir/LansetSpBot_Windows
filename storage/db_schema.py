from __future__ import annotations

from storage.sqlcipher_driver import dbapi as sqlite3
from pathlib import Path
from typing import ContextManager, TYPE_CHECKING

from storage.schema import (
    LegacySchemaMigrationMixin,
    SchemaBootstrapMixin,
    SchemaCoreMixin,
    SchemaV14MigrationMixin,
    SchemaV15MigrationMixin,
)


class DatabaseSchemaMixin(
    SchemaCoreMixin,
    LegacySchemaMigrationMixin,
    SchemaV14MigrationMixin,
    SchemaV15MigrationMixin,
    SchemaBootstrapMixin,
):
    """Compatibility facade for schema bootstrap and forward migrations."""

    SCHEMA_VERSION = 30
    LEGACY_SCHEMA_VERSION = 13

    if TYPE_CHECKING:
        path: Path
        sqlite_timeout_seconds: float
        busy_timeout_ms: int

        def get_connection(self) -> ContextManager[sqlite3.Connection]: ...
