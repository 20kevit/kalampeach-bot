#!/usr/bin/env python3
"""Create the data directory (0700) and apply pending migrations.

Safe to run repeatedly. Never drops, truncates or resets existing data.

Usage (OpenCode, on cPanel)::

    ~/virtualenv/APP_ROOT/3.11/bin/python scripts/init_db.py --env-file ~/private/kalampeach.env

Exit codes: 0 = schema current, 1 = failure (details on stderr, no secrets).
"""

from __future__ import annotations

import json
import sys

from _common import build_parser, context_from_args, fail


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Initialise or migrate the KalamPeach bot database")
    parser.add_argument("--show-version", action="store_true", help="only print the current schema version")
    args = parser.parse_args(argv)

    try:
        ctx = context_from_args(args)
    except Exception as exc:  # noqa: BLE001 - configuration problems are reported verbatim
        return fail(f"configuration invalid: {exc}")

    try:
        if args.show_version:
            print(json.dumps({"schema_version": ctx.db.version()}))
            return 0
        applied = ctx.db.ensure_schema()
        version = ctx.db.version()
    except Exception as exc:  # noqa: BLE001
        return fail(f"migration failed ({type(exc).__name__}): {exc}")

    print(
        json.dumps(
            {
                "database_path": ctx.config.database_path,
                "applied_versions": applied,
                "schema_version": version,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
