#!/usr/bin/env python3
"""Inspect report deliveries and resolve the rare ``uncertain`` state.

A timed-out Telegram send is recorded as ``uncertain`` and is never retried
automatically, because a blind retry could duplicate the manager's report. The
operator checks the manager chat:

* report did arrive  → ``--resolve KEY --as sent``
* report did not arrive → ``--resolve KEY --as retry`` (one deliberate resend)
"""

from __future__ import annotations

import json
import sys

from _common import build_parser, context_from_args, fail

from app.db import transaction
from app.repo import reports
from app.timeutil import to_utc_iso, utc_now


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Inspect and resolve report deliveries")
    parser.add_argument("--list", type=int, default=20, help="show the most recent N deliveries")
    parser.add_argument("--show", default=None, help="show one delivery by report key")
    parser.add_argument("--resolve", default=None, help="report key in uncertain state")
    parser.add_argument("--as", dest="resolve_as", choices=("sent", "retry"), default=None)
    parser.add_argument("--confirm", action="store_true", help="required for --resolve")
    args = parser.parse_args(argv)

    try:
        ctx = context_from_args(args)
    except Exception as exc:  # noqa: BLE001
        return fail(f"configuration invalid: {exc}")

    with ctx.db.session() as conn:
        if args.show:
            row = reports.get(conn, args.show)
            if row is None:
                return fail("report key not found")
            print(json.dumps(dict(row), ensure_ascii=False, indent=2))
            return 0

        if not args.resolve:
            rows = reports.list_recent(conn, limit=args.list)
            print(
                json.dumps(
                    [
                        {
                            "report_key": row["report_key"],
                            "type": row["report_type"],
                            "period": f"{row['period_start_date']}..{row['period_end_date']}",
                            "status": row["status"],
                            "attempts": row["attempts"],
                            "last_error": row["last_error_category"],
                            "sent_at_utc": row["sent_at_utc"],
                        }
                        for row in rows
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if not args.resolve_as:
            return fail("--resolve requires --as sent|retry")
        if not args.confirm:
            return fail("refusing to change delivery state without --confirm")

        row = reports.get(conn, args.resolve)
        if row is None:
            return fail("report key not found")
        if str(row["status"]) != reports.STATUS_UNCERTAIN:
            return fail(f"report is {row['status']}, not uncertain")

        now_iso = to_utc_iso(utc_now())
        with transaction(conn):
            if args.resolve_as == "sent":
                conn.execute(
                    "UPDATE report_deliveries SET status = ?, last_error_category = ?, "
                    "  updated_at_utc = ?, sent_at_utc = COALESCE(sent_at_utc, ?) "
                    "WHERE report_key = ?",
                    (reports.STATUS_SENT, "OPERATOR_CONFIRMED_SENT", now_iso, now_iso, args.resolve),
                )
            else:
                conn.execute(
                    "UPDATE report_deliveries SET status = ?, next_attempt_at_utc = ?, "
                    "  last_error_category = ?, updated_at_utc = ?, attempts = 0 "
                    "WHERE report_key = ?",
                    (
                        reports.STATUS_PENDING,
                        now_iso,
                        "OPERATOR_RETRY_REQUESTED",
                        now_iso,
                        args.resolve,
                    ),
                )
        print(json.dumps({"report_key": args.resolve, "resolved_as": args.resolve_as}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
