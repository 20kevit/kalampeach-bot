#!/usr/bin/env python3
"""Cron worker for the KalamPeach Telegram bot.

Designed for cPanel cron, ideally every minute::

    * * * * * /home/CPANEL_USER/virtualenv/APP/3.11/bin/python \
        /home/CPANEL_USER/kalampeach/scripts/run_worker.py \
        >> /home/CPANEL_USER/logs/kalampeach-worker.log 2>&1

One invocation:

1. applies pending forward-only migrations (idempotent, overlap-safe);
2. drains a bounded batch of queued Telegram updates with leases and retries;
3. delivers due clarification questions;
4. sends the 23:30 Asia/Tehran daily report (and recovers a missed tick once);
5. retries due report deliveries;
6. exits cleanly with a one-line JSON summary on stdout.

Nothing is deployed or started by this repository's coding phase; this command
is what OpenCode will schedule during the approved deployment phase.
"""

from __future__ import annotations

import argparse
import json
import sys

from _bootstrap import EXIT_CONFIG, EXIT_DATABASE, EXIT_OK, EXIT_FAILURE

from kalampeach.config import ConfigError, load_settings
from kalampeach.db import DatabaseError
from kalampeach.logging_utils import configure_logging
from kalampeach.services.context import build_context
from kalampeach.services.worker import Worker


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="KalamPeach cron worker (bounded, idempotent run).")
    parser.add_argument("--batch-size", type=int, default=None, help="max updates per run")
    parser.add_argument("--max-seconds", type=float, default=None, help="max wall-clock seconds per run")
    parser.add_argument("--json", action="store_true", help="print the run summary as JSON (default)")
    parser.add_argument("--quiet", action="store_true", help="suppress the JSON summary line")
    parser.add_argument("--skip-reports", action="store_true", help="process updates only")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print("configuration error:", file=sys.stderr)
        for problem in exc.problems:
            print(" - %s" % problem, file=sys.stderr)
        print("Run `python scripts/doctor.py` for a full check.", file=sys.stderr)
        return EXIT_CONFIG

    logger = configure_logging(settings.log_level, settings.redaction_secrets())

    try:
        ctx = build_context(settings)
        worker = Worker(ctx, batch_size=args.batch_size, max_seconds=args.max_seconds)
        summary = worker.run_once(skip_reports=args.skip_reports)
    except DatabaseError as exc:
        logger.error("worker_database_error error=%s", exc.__class__.__name__)
        print("database error: %s" % exc, file=sys.stderr)
        return EXIT_DATABASE
    except Exception as exc:  # pragma: no cover - last-resort guard for cron
        logger.exception("worker_fatal error=%s", exc.__class__.__name__)
        return EXIT_FAILURE

    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
