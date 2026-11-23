"""Forward-only, checksum-verified schema migrations.

Rules (see AGENTS.md / DEPLOYMENT.md):

* migrations are additive and never drop or reset production data;
* each migration runs in its own ``BEGIN IMMEDIATE`` transaction;
* the runner is safe under overlapping cron invocations because the applied
  version list is re-read inside the write transaction;
* an already-applied migration whose SQL text changed raises an error instead of
  being silently re-applied.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from ..timeutil import to_utc_iso, utc_now

logger = logging.getLogger("kalampeach.migrations")

SQL_DIR = Path(__file__).resolve().parent / "sql"

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version        INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    applied_at_utc TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Raised when migrations cannot be applied safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load_migrations(directory: Path = SQL_DIR) -> List[Migration]:
    """Load ``NNN_name.sql`` files in ascending version order."""

    if not directory.is_dir():
        raise MigrationError("migration directory is missing: %s" % directory)
    migrations: List[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        stem = path.stem
        prefix, _, name = stem.partition("_")
        if not prefix.isdigit():
            raise MigrationError("migration filename must start with a number: %s" % path.name)
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise MigrationError("migration file is empty: %s" % path.name)
        migrations.append(Migration(version=int(prefix), name=name or stem, sql=sql))

    versions = [migration.version for migration in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration version numbers: %s" % versions)
    migrations.sort(key=lambda migration: migration.version)
    return migrations


def applied_versions(connection: sqlite3.Connection) -> dict:
    try:
        rows = connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    except sqlite3.Error:
        return {}
    return {int(row[0]): str(row[1]) for row in rows}


def latest_version() -> int:
    """Highest migration version shipped with this codebase."""

    migrations = load_migrations()
    return max((migration.version for migration in migrations), default=0)


def current_version(connection: sqlite3.Connection) -> int:
    versions = applied_versions(connection)
    return max(versions) if versions else 0


def _ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(_MIGRATION_TABLE)


def _split_statements(sql: str) -> List[str]:
    """Split a SQL script into complete statements without breaking on quotes."""

    statements: List[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    trailing = buffer.strip()
    if trailing:
        statements.append(trailing)
    return statements


def apply_migrations(connection: sqlite3.Connection, migrations: Sequence[Migration]) -> List[Migration]:
    """Apply every pending migration; return the ones applied in this call.

    The caller supplies a connection; this function manages its own
    transactions so that each migration is atomic. Statements are executed
    individually (``executescript`` would implicitly commit and break
    atomicity).
    """

    _ensure_table(connection)
    applied = applied_versions(connection)
    pending = [migration for migration in migrations if migration.version not in applied]

    # Verify checksums of already-applied migrations before touching anything.
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded and recorded != migration.checksum:
            raise MigrationError(
                "migration %03d_%s was modified after being applied; create a new "
                "migration instead" % (migration.version, migration.name)
            )

    if not pending:
        return []

    applied_now: List[Migration] = []
    for migration in pending:
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Re-check inside the lock so overlapping processes cannot double-apply.
            fresh = applied_versions(connection)
            if migration.version in fresh:
                connection.execute("ROLLBACK")
                continue
            for statement in _split_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    to_utc_iso(utc_now()),
                ),
            )
            connection.execute("COMMIT")
        except Exception as exc:
            connection.execute("ROLLBACK")
            raise MigrationError(
                "migration %03d_%s failed: %s"
                % (migration.version, migration.name, exc.__class__.__name__)
            ) from exc
        applied_now.append(migration)
        logger.info(
            "migration_applied version=%03d name=%s", migration.version, migration.name
        )
    return applied_now


def ensure_schema(connection: sqlite3.Connection, directory: Path = SQL_DIR) -> int:
    """Apply pending migrations and return the resulting schema version."""

    apply_migrations(connection, load_migrations(directory))
    return current_version(connection)


def pending_migrations(connection: sqlite3.Connection, directory: Path = SQL_DIR) -> List[Migration]:
    _ensure_table(connection)
    applied = applied_versions(connection)
    return [m for m in load_migrations(directory) if m.version not in applied]


def describe(connection: sqlite3.Connection, directory: Path = SQL_DIR) -> List[Tuple[Migration, bool]]:
    """Return ``(migration, is_applied)`` pairs for the doctor/status output."""

    _ensure_table(connection)
    applied = applied_versions(connection)
    return [(migration, migration.version in applied) for migration in load_migrations(directory)]
