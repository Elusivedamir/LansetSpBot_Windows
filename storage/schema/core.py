from __future__ import annotations

from typing import TYPE_CHECKING

import logging
from storage.sqlcipher_driver import dbapi as sqlite3
import time

from core.performance import log_if_slow
from storage.db_common import DatabaseError
from storage.db_target_schema import migrate_comment_targets_v16
from storage.migrations.comment_cadence_v17 import migrate_comment_cadence_v17
from storage.migrations.account_isolation_v18 import migrate_account_isolation_v18
from storage.migrations.account_restrictions_v19 import migrate_account_restrictions_v19
from storage.migrations.comment_variants_v20 import migrate_comment_variants_v20
from storage.migrations.comment_delivery_context_v21 import (
    migrate_comment_delivery_context_v21,
)
from storage.migrations.link_state_v22 import migrate_link_state_v22
from storage.migrations.rpc_optimization_v23 import migrate_rpc_optimization_v23
from storage.migrations.comment_delivery_source_v24 import (
    migrate_comment_delivery_source_v24,
)
from storage.migrations.comment_only_v25 import migrate_comment_only_v25
from storage.migrations.rpc_cooldown_boot_guard_v26 import (
    migrate_rpc_cooldown_boot_guard_v26,
)
from storage.migrations.local_channel_ban_v27 import migrate_local_channel_ban_v27
from storage.migrations.safety_invariants_v28 import migrate_safety_invariants_v28
from storage.migrations.activity_log_account_scope_v29 import (
    migrate_activity_log_account_scope_v29,
)
from storage.migrations.openai_comments_v30 import migrate_openai_comments_v30
from storage.migrations.multiaccount_v31 import migrate_multiaccount_v31

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class SchemaCoreMixin(_MixinHost):
    def _raw_user_version(self):
        connection = sqlite3.connect(
            str(self.path), timeout=self.sqlite_timeout_seconds,
            key=self._database_key, key_storage_dir=self.key_storage_dir
        )
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def _ensure_wal_mode(self) -> str:
        """Persist WAL mode before GUI and worker connections start.

        ``journal_mode`` is a database-file setting, but older/copied databases
        may still be in DELETE mode. Bootstrap owns the one safe point where the
        application can switch the file before the queue worker opens it.
        """

        started = time.monotonic()
        connection = sqlite3.connect(
            str(self.path), timeout=self.sqlite_timeout_seconds,
            key=self._database_key, key_storage_dir=self.key_storage_dir
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = str(row[0] if row else "").strip().lower()
            if mode != "wal":
                raise DatabaseError(
                    f"SQLite WAL mode could not be enabled for {self.path}; current mode={mode or 'unknown'}"
                )
            return mode
        except sqlite3.Error as exc:
            raise DatabaseError(f"Unable to enable SQLite WAL mode: {exc}") from exc
        finally:
            connection.close()
            log_if_slow(
                log,
                "sqlite_enable_wal",
                started,
                threshold_seconds=0.5,
            )

    def get_version(self):
        with self.get_connection() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def set_version(self, version):
        with self.get_connection() as conn:
            conn.execute(f"PRAGMA user_version = {int(version)}")

    def health_check(self) -> dict[str, object]:
        """Return a read-only snapshot of the local SQLite state.

        Marlen reuses one SQLite connection per owning thread. WAL, busy_timeout
        and foreign keys allow the GUI and queue worker to safely share the same
        local database file without ever sharing a connection across threads.
        """
        with self.get_connection() as conn:
            quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            foreign_keys = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return {
            "path": str(self.path.resolve()),
            "quick_check": quick_check,
            "journal_mode": journal_mode.lower(),
            "foreign_keys": foreign_keys,
            "user_version": user_version,
            "local_file": True,
        }

    def run_migrations(self):
        """Upgrade only versions that have not yet been applied."""
        current_version = self.get_version()
        if current_version > self.SCHEMA_VERSION:
            raise DatabaseError(
                f"Database schema v{current_version} is newer than supported v{self.SCHEMA_VERSION}"
            )
        if current_version < self.LEGACY_SCHEMA_VERSION:
            self._upgrade_legacy_to_v13()
            current_version = self.get_version()
        if current_version < 14:
            self._migrate_to_v14()
            current_version = self.get_version()
        if current_version < 15:
            self._migrate_to_v15()
            current_version = self.get_version()
        if current_version < 16:
            migrate_comment_targets_v16(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 17:
            migrate_comment_cadence_v17(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 18:
            migrate_account_isolation_v18(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 19:
            migrate_account_restrictions_v19(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 20:
            migrate_comment_variants_v20(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 21:
            migrate_comment_delivery_context_v21(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 22:
            migrate_link_state_v22(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 23:
            migrate_rpc_optimization_v23(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 24:
            migrate_comment_delivery_source_v24(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 25:
            migrate_comment_only_v25(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 26:
            migrate_rpc_cooldown_boot_guard_v26(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 27:
            migrate_local_channel_ban_v27(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 28:
            migrate_safety_invariants_v28(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 29:
            migrate_activity_log_account_scope_v29(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 30:
            migrate_openai_comments_v30(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            current_version = self.get_version()
        if current_version < 31:
            migrate_multiaccount_v31(
                self.path,
                sqlite_timeout_seconds=self.sqlite_timeout_seconds,
                busy_timeout_ms=self.busy_timeout_ms,
            )
        log.info("Database schema version: %s", self.get_version())
