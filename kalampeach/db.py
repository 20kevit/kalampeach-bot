"""SQLite access for the independent bot database.

* one short-lived connection per request/cron run (shared hosting friendly);
* foreign keys enforced on every connection;
* explicit ``BEGIN IMMEDIATE`` transactions for writes that must not interleave
  (event creation, corrections, lease claims, report state changes);
* the database file and its directory live outside the public web root.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger("kalampeach.db")


class DatabaseError(RuntimeError):
    """Raised when the database cannot be opened or is not healthy."""


class Database:
    """Thin wrapper around :mod:`sqlite3` with safe defaults."""

    def __init__(
        self,
        path: object,
        *,
        busy_timeout_ms: int = 5000,
        journal_mode: str = "WAL",
        create_dirs: bool = True,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.journal_mode = str(journal_mode).upper()
        self.create_dirs = create_dirs
        self._journal_mode_applied: Optional[str] = None

    # -- connection management -------------------------------------------
    def _ensure_directory(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            if not self.create_dirs:
                raise DatabaseError("database directory does not exist: %s" % parent)
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not os.access(parent, os.W_OK):
            raise DatabaseError("database directory is not writable by this process: %s" % parent)

    def connect(self) -> sqlite3.Connection:
        if not str(self.path):
            raise DatabaseError("DATABASE_PATH is empty")
        self._ensure_directory()
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.busy_timeout_ms / 1000.0,
                isolation_level=None,  # explicit transaction control
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise DatabaseError("cannot open database: %s" % exc.__class__.__name__) from exc

        self._restrict_permissions()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = %d" % self.busy_timeout_ms)
        if self._journal_mode_applied is None:
            try:
                mode = connection.execute("PRAGMA journal_mode = %s" % self.journal_mode)
                applied = (mode.fetchone() or [""])[0]
                self._journal_mode_applied = str(applied).upper()
                if self._journal_mode_applied != self.journal_mode:
                    logger.warning(
                        "journal_mode_unavailable requested=%s applied=%s",
                        self.journal_mode,
                        self._journal_mode_applied,
                    )
            except sqlite3.Error as exc:  # pragma: no cover - host dependent
                logger.warning("journal_mode_error error=%s", exc.__class__.__name__)
        return connection

    def _restrict_permissions(self) -> None:
        """Keep the SQLite file private on shared hosting (best effort)."""

        try:
            if self.path.is_file():
                mode = self.path.stat().st_mode & 0o777
                if mode & 0o077:
                    os.chmod(self.path, 0o600)
        except OSError as exc:  # pragma: no cover - host permissions
            logger.warning("database_chmod_failed error=%s", exc.__class__.__name__)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _review_needed(connection: sqlite3.Connection) -> dict:
        """Rows whose delivery outcome is unknown and need an operator look.

        Counts only; no message content. Missing tables simply report zero.
        """

        counts = {"clarifications_uncertain": 0, "reports_uncertain": 0}
        queries = {
            "clarifications_uncertain": "SELECT COUNT(*) FROM clarifications WHERE status = 'delivery_uncertain'",
            "reports_uncertain": "SELECT COUNT(*) FROM report_deliveries WHERE status = 'failed_uncertain'",
        }
        for key, sql in queries.items():
            try:
                counts[key] = int(connection.execute(sql).fetchone()[0])
            except sqlite3.Error:
                counts[key] = 0
        return counts

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a block inside a transaction; roll back on any exception."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()

    # -- diagnostics ------------------------------------------------------
    def health(self) -> dict:
        """Return safe diagnostics for ``/health`` and ``doctor``."""

        info = {"path_configured": bool(str(self.path)), "ok": False, "schema_version": 0}
        try:
            with self.connection() as connection:
                info["ok"] = connection.execute("SELECT 1").fetchone() is not None
                try:
                    row = connection.execute(
                        "SELECT MAX(version) AS version FROM schema_migrations"
                    ).fetchone()
                    info["schema_version"] = int(row["version"] or 0) if row else 0
                except sqlite3.Error:
                    info["schema_version"] = 0
                info["integrity"] = connection.execute("PRAGMA quick_check").fetchone()[0]
                info["review_needed"] = self._review_needed(connection)
        except (DatabaseError, sqlite3.Error) as exc:
            info["error"] = exc.__class__.__name__
        return info
