from __future__ import annotations

from pathlib import Path
from typing import ContextManager, TYPE_CHECKING

from storage.schema import (
    LegacySchemaMigrationMixin,
    SchemaBootstrapMixin,
    SchemaCoreMixin,
    SchemaV14MigrationMixin,
    SchemaV15MigrationMixin,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``sqlite3`` is bound to the SQLCipher DBAPI proxy object, not to a
    # module, so its DBAPI classes are imported from the standard library
    # for annotations. The two drivers are DBAPI-compatible.
    from sqlite3 import Connection as SQLiteConnection


class DatabaseSchemaMixin(
    SchemaCoreMixin,
    LegacySchemaMigrationMixin,
    SchemaV14MigrationMixin,
    SchemaV15MigrationMixin,
    SchemaBootstrapMixin,
):
    """Compatibility facade for schema bootstrap and forward migrations."""

    SCHEMA_VERSION = 31
    LEGACY_SCHEMA_VERSION = 13

    if TYPE_CHECKING:
        path: Path
        sqlite_timeout_seconds: float
        busy_timeout_ms: int

        def get_connection(self) -> ContextManager[SQLiteConnection]: ...
