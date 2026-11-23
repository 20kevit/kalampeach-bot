#!/usr/bin/env python3
"""Consistent SQLite backup with verification.

Uses SQLite's online backup API (safe while the webhook is writing), writes the
copy with mode 0600 into a private directory, then opens the copy and runs
``PRAGMA integrity_check`` plus row counts.

Restore procedure is documented in CPANEL-DEPLOYMENT.md; this script never
overwrites the live database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from _common import build_parser, context_from_args, fail


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.Error:
        return -1
    return int(row[0]) if row else -1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Back up the KalamPeach bot database")
    parser.add_argument("--dest", required=True, help="private backup directory (absolute, outside public_html)")
    args = parser.parse_args(argv)

    try:
        ctx = context_from_args(args)
    except Exception as exc:  # noqa: BLE001
        return fail(f"configuration invalid: {exc}")

    source_path = Path(ctx.config.database_path)
    if not source_path.is_file():
        return fail("database file does not exist; run scripts/init_db.py first")

    dest_dir = Path(args.dest)
    if "public_html" in {part.lower() for part in dest_dir.parts}:
        return fail("backup directory must not be inside public_html")
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest_dir, 0o700)
    except OSError:
        pass

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"kalampeach_bot-{stamp}.sqlite3"

    try:
        source = sqlite3.connect(str(source_path))
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(target, 0o600)
    except (sqlite3.Error, OSError) as exc:
        return fail(f"backup failed ({type(exc).__name__}): {exc}")

    try:
        check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {
                table: _count(check, table)
                for table in (
                    "events",
                    "event_revisions",
                    "source_messages",
                    "clarifications",
                    "report_deliveries",
                    "telegram_inbox",
                )
            }
            version_row = check.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            schema_version = version_row[0] if version_row else None
        finally:
            check.close()
    except sqlite3.Error as exc:
        return fail(f"backup verification failed ({type(exc).__name__}): {exc}")

    if integrity != "ok":
        return fail(f"backup integrity check returned: {integrity}")

    print(
        json.dumps(
            {
                "backup_path": str(target),
                "bytes": target.stat().st_size,
                "integrity": integrity,
                "schema_version": schema_version,
                "row_counts": counts,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
