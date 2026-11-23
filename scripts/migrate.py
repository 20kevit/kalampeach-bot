#!/usr/bin/env python3
"""Apply or inspect the forward-only database schema migrations.

Never destructive: migrations only add structures, each runs inside its own
``BEGIN IMMEDIATE`` transaction, and an already-applied migration whose SQL text
changed raises an error instead of being re-applied.

Examples::

    python scripts/migrate.py --status
    python scripts/migrate.py --apply
    python scripts/migrate.py --dry-run
"""

from __future__ import annotations

import argparse
import sys

from _bootstrap import EXIT_CONFIG, EXIT_DATABASE, EXIT_OK

from kalampeach.config import ConfigError, load_settings
from kalampeach.db import Database, DatabaseError
from kalampeach.logging_utils import configure_logging
from kalampeach.migrations import (
    MigrationError,
    apply_migrations,
    describe,
    load_migrations,
    pending_migrations,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="KalamPeach database migrations.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="apply pending migrations (default)")
    group.add_argument("--status", action="store_true", help="show applied/pending migrations only")
    group.add_argument("--dry-run", action="store_true", help="show what would be applied")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("configuration error:", file=sys.stderr)
        for problem in exc.problems:
            print(" - %s" % problem, file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(settings.log_level, settings.redaction_secrets())
    db = Database(
        settings.database_path,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
        journal_mode=settings.sqlite_journal_mode,
    )

    try:
        with db.connection() as connection:
            if args.status:
                for migration, applied in describe(connection):
                    print(
                        "%03d %-40s %s"
                        % (migration.version, migration.name, "applied" if applied else "PENDING")
                    )
                return EXIT_OK

            pending = pending_migrations(connection)
            if args.dry_run:
                if not pending:
                    print("no pending migrations")
                for migration in pending:
                    print("would apply %03d %s" % (migration.version, migration.name))
                return EXIT_OK

            applied = apply_migrations(connection, load_migrations())
            if not applied:
                print("database already up to date")
            for migration in applied:
                print("applied %03d %s" % (migration.version, migration.name))
            return EXIT_OK
    except (DatabaseError, MigrationError) as exc:
        print("migration failed: %s" % exc, file=sys.stderr)
        return EXIT_DATABASE


if __name__ == "__main__":
    raise SystemExit(main())
