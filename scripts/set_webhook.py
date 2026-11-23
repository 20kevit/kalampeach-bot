#!/usr/bin/env python3
"""Register or inspect the Telegram webhook.

LIVE EXTERNAL CHANGE. This script is for the OpenCode operations phase only and
refuses to run without ``--confirm``. Arena never executes it.

Typical flow (after owner approval and HTTPS verification)::

    python scripts/set_webhook.py --env-file ~/private/kalampeach.env --info
    python scripts/set_webhook.py --env-file ~/private/kalampeach.env --confirm
    python scripts/set_webhook.py --env-file ~/private/kalampeach.env --delete --confirm
"""

from __future__ import annotations

import json
import sys

from _common import build_parser, context_from_args, fail


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Inspect or change the Telegram webhook (live external change)")
    parser.add_argument("--info", action="store_true", help="print current webhook info and exit")
    parser.add_argument("--delete", action="store_true", help="delete the webhook")
    parser.add_argument("--url", default=None, help="override WEBHOOK_PUBLIC_URL")
    parser.add_argument("--confirm", action="store_true", help="required for any change")
    args = parser.parse_args(argv)

    try:
        ctx = context_from_args(args)
    except Exception as exc:  # noqa: BLE001
        return fail(f"configuration invalid: {exc}")

    if args.info:
        info = ctx.telegram.get_webhook_info()
        safe = {
            "url": info.get("url"),
            "pending_update_count": info.get("pending_update_count"),
            "last_error_date": info.get("last_error_date"),
            "last_error_message": info.get("last_error_message"),
            "max_connections": info.get("max_connections"),
            "allowed_updates": info.get("allowed_updates"),
        }
        print(json.dumps(safe, ensure_ascii=False))
        return 0

    if not args.confirm:
        return fail("refusing to change the live webhook without --confirm (owner approval required)")

    if args.delete:
        print(json.dumps(ctx.telegram.delete_webhook(drop_pending_updates=False)))
        return 0

    url = args.url or ctx.config.webhook_public_url
    if not url.startswith("https://"):
        return fail("webhook URL must be https://")
    result = ctx.telegram.set_webhook(
        url=url, secret_token=ctx.config.telegram_webhook_secret, allowed_updates=["message"]
    )
    print(json.dumps({"set_webhook": result, "url": url}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
