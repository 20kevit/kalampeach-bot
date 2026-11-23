#!/usr/bin/env python3
"""Safe online backup of the bot's SQLite database.

Uses SQLite's online backup API (consistent snapshot while the app may be
running), verifies the copy with ``PRAGMA integrity_check``, sets ``0600``
permissions, and prunes old backups by count.

Restore is deliberately manual (see CPANEL-DEPLOYMENT.md): stop cron, copy the
chosen backup over the live path, run ``scripts/migrate.py --status``, then
re-enable cron. This script never overwrites the live database.

Example::

    python scripts/backup_db.py --output /home/CPANEL_USER/backups --keep 14
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import EXIT_CONFIG, EXIT_DATABASE, EXIT_FAILURE, EXIT_OK

from kalampeach.config import ConfigError, load_settings
from kalampeach.logging_utils import configure_logging

PUBLIC_DIR_NAMES = {"public_html", "public_www", "www", "public"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Back up the KalamPeach SQLite database safely.")
    parser.add_argument("--output", required=True, help="private backup directory (outside public_html)")
    parser.add_argument("--keep", type=int, default=14, help="number of most recent backups to keep")
    parser.add_argument("--database", default=None, help="override DATABASE_PATH")
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

    source = Path(args.database) if args.database else Path(settings.database_path)
    if not source.is_file():
        print("database file does not exist: %s" % source, file=sys.stderr)
        return EXIT_DATABASE

    output_dir = Path(args.output).expanduser()
    if {part.lower() for part in output_dir.parts} & PUBLIC_DIR_NAMES:
        print("refusing to write backups inside the public web root", file=sys.stderr)
        return EXIT_CONFIG
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = output_dir / ("kalampeach_bot_%s.sqlite3" % timestamp)
    if destination.exists():
        print("backup destination already exists: %s" % destination, file=sys.stderr)
        return EXIT_FAILURE

    try:
        with sqlite3.connect("file:%s?mode=ro" % source, uri=True) as source_connection:
            with sqlite3.connect(str(destination)) as destination_connection:
                source_connection.backup(destination_connection)
        with sqlite3.connect(str(destination)) as check_connection:
            result = check_connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        print("backup failed: %s" % exc.__class__.__name__, file=sys.stderr)
        if destination.exists():
            destination.unlink(missing_ok=True)
        return EXIT_DATABASE

    if str(result).lower() != "ok":
        print("backup integrity check failed: %s" % result, file=sys.stderr)
        destination.unlink(missing_ok=True)
        return EXIT_FAILURE

    os.chmod(destination, 0o600)
    size = destination.stat().st_size
    print("backup_created path=%s bytes=%d integrity=ok" % (destination, size))

    _prune(output_dir, keep=max(1, args.keep), current=destination)
    return EXIT_OK


def _prune(output_dir: Path, *, keep: int, current: Path) -> None:
    backups = sorted(output_dir.glob("kalampeach_bot_*.sqlite3"), key=lambda path: path.name)
    if len(backups) <= keep:
        return
    for stale in backups[: len(backups) - keep]:
        if stale == current:
            continue
        try:
            stale.unlink()
            print("backup_pruned path=%s" % stale)
        except OSError as exc:  # pragma: no cover - permissions
            print("backup_prune_failed path=%s error=%s" % (stale, exc.__class__.__name__), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
