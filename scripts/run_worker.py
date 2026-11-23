#!/usr/bin/env python3
"""Cron-invoked worker: drain the inbox and deliver due reports, then exit.

Intended cPanel cron entry (every minute, real paths required)::

    * * * * * /home/CPANEL_USER/virtualenv/APP_ROOT/3.11/bin/python \
        /home/CPANEL_USER/APP_ROOT/scripts/run_worker.py \
        --env-file /home/CPANEL_USER/private/kalampeach.env \
        >> /home/CPANEL_USER/logs/kalampeach-worker.log 2>&1

The worker is safe under overlapping invocations: claims are database leases.
A bounded runtime keeps it inside shared-hosting limits; anything not finished
returns to the queue immediately.
"""

from __future__ import annotations

import json
import sys

from _common import build_parser, context_from_args, fail
from app.db import require_current_schema
from app.services.worker import run_worker


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Process queued Telegram updates and due reports")
    parser.add_argument("--max-runtime", type=int, default=None, help="seconds; default from config")
    parser.add_argument("--batch-size", type=int, default=None, help="updates per run; default from config")
    parser.add_argument(
        "--no-reports", action="store_true", help="skip due-report evaluation (diagnostics only)"
    )
    args = parser.parse_args(argv)

    try:
        ctx = context_from_args(args)
    except Exception as exc:  # noqa: BLE001
        return fail(f"configuration invalid: {exc}")

    try:
        with ctx.db.session() as conn:
            require_current_schema(conn)
            stats = run_worker(
                ctx,
                conn,
                max_runtime_seconds=args.max_runtime,
                batch_size=args.batch_size,
                collect_reports=not args.no_reports,
            )
    except Exception as exc:  # noqa: BLE001
        return fail(f"worker failed ({type(exc).__name__}): {exc}")

    print(json.dumps({"worker": stats.as_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
