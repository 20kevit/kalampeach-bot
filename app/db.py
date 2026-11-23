"""SQLite persistence and schema migrations.

The database file lives at ``DATABASE_PATH`` (absolute, outside any public web
root). No ORM: a small explicit SQL layer keeps the shared-hosting footprint
tiny and makes every transaction reviewable.

Concurrency model: SQLite serialises writers. Every write path uses a short
``BEGIN IMMEDIATE`` transaction, a busy timeout and row-level claims, so a
Passenger web process and one or more overlapping cron workers cannot corrupt
state. WAL journaling improves concurrency where the host filesystem supports
it; if the ``PRAGMA`` is refused the connection falls back to the host default
and the worker still works (documented in CPANEL-DEPLOYMENT.md).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.errors import SchemaError

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
BUSY_TIMEOUT_MS = 5000
REQUIRED_SCHEMA_VERSION = 1


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # Some network/shared filesystems refuse WAL. Correctness does not
        # depend on it; keep the default journal mode.
        pass
    try:
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError:
        pass


def connect(database_path: str, *, create_dir: bool = False) -> sqlite3.Connection:
    """Open a configured connection. Callers own closing it."""

    path = Path(database_path)
    if create_dir:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass
    if not path.parent.exists():
        raise SchemaError(
            f"database directory does not exist: {path.parent}; create it with "
            "scripts/init_db.py"
        )
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """Short write transaction. Rolls back on any exception."""

    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migration_files() -> list[tuple[int, Path]]:
    files: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        prefix = path.name.split("_", 1)[0]
        try:
            version = int(prefix)
        except ValueError as exc:  # pragma: no cover - repository invariant
            raise SchemaError(f"migration file name is invalid: {path.name}") from exc
        files.append((version, path))
    return files


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["version"] or 0) if row else 0


def pending_versions(conn: sqlite3.Connection) -> list[int]:
    current = schema_version(conn)
    return [version for version, _ in _migration_files() if version > current]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order, each in its own transaction.

    Migrations are additive and idempotent by convention; nothing here ever
    drops or resets data. A concurrent migration attempt is serialised by
    SQLite locking and re-checked inside the transaction.
    """

    applied: list[int] = []
    for version, path in _migration_files():
        with transaction(conn):
            current = schema_version(conn)
            if version <= current:
                continue
            sql = path.read_text(encoding="utf-8")
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at_utc) "
                "VALUES (?, ?, ?)",
                (version, path.name, _utc_now_iso()),
            )
        applied.append(version)
    return applied


def _utc_now_iso() -> str:
    from app.timeutil import to_utc_iso, utc_now

    return to_utc_iso(utc_now())


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into statements.

    Simple semicolon splitting is sufficient because migrations contain no
    triggers or string literals with semicolons; an assertion keeps that true.
    """

    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            buffer = []
    leftover = "\n".join(buffer).strip()
    if leftover:
        statements.append(leftover)
    for statement in statements:
        if ";" in statement:
            raise SchemaError("migration contains an unsupported embedded semicolon")
    return statements


def require_current_schema(conn: sqlite3.Connection) -> None:
    version = schema_version(conn)
    if version < REQUIRED_SCHEMA_VERSION:
        raise SchemaError(
            "database schema is not initialised (found version "
            f"{version}, need {REQUIRED_SCHEMA_VERSION}); run scripts/init_db.py"
        )


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "unknown"


class Database:
    """Thin factory so each web request and worker run gets its own connection."""

    def __init__(self, path: str) -> None:
        self.path = path

    def connect(self, *, create_dir: bool = False) -> sqlite3.Connection:
        return connect(self.path, create_dir=create_dir)

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> list[int]:
        """Apply pending migrations. Only scripts call this, never a request."""

        conn = self.connect(create_dir=True)
        try:
            return migrate(conn)
        finally:
            conn.close()

    def version(self) -> int:
        with self.session() as conn:
            return schema_version(conn)
