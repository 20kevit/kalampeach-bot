#!/usr/bin/env python3
"""Inspect or (with explicit confirmation) change the live Telegram webhook.

**This is an operations-phase tool.** Arena.ai never runs it; OpenCode uses it
only after the owner approves live changes. Reads are harmless; ``set`` and
``delete`` require ``--yes`` because they change live Telegram state.

Examples::

    python scripts/telegram_webhook.py info
    python scripts/telegram_webhook.py set --yes
    python scripts/telegram_webhook.py delete --yes

The bot token is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys

from _bootstrap import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK

from kalampeach.config import ConfigError, load_settings
from kalampeach.logging_utils import configure_logging
from kalampeach.telegram.client import TelegramClient, TelegramError

ALLOWED_UPDATES = ["message"]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Telegram webhook status / registration.")
    parser.add_argument("command", choices=["info", "set", "delete"])
    parser.add_argument("--yes", action="store_true", help="confirm a live change (required for set/delete)")
    parser.add_argument(
        "--allow-updates",
        default=",".join(ALLOWED_UPDATES),
        help="comma-separated allowed_updates for set (default: message)",
    )
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
    client = TelegramClient(settings)

    if args.command in ("set", "delete") and not args.yes:
        print(
            "refusing to change live Telegram state without --yes.\n"
            "Review the deployment checklist in CPANEL-DEPLOYMENT.md first.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    try:
        if args.command == "info":
            info = client.get_webhook_info()
            print(json.dumps(_redact_info(info), ensure_ascii=False, indent=2, sort_keys=True))
            return EXIT_OK
        if args.command == "set":
            updates = [item.strip() for item in args.allow_updates.split(",") if item.strip()]
            client.set_webhook(
                settings.webhook_public_url, settings.webhook_secret, allowed_updates=updates
            )
            print("webhook_registered url=%s allowed_updates=%s" % (
                settings.webhook_public_url,
                ",".join(updates),
            ))
            info = client.get_webhook_info()
            print(json.dumps(_redact_info(info), ensure_ascii=False, indent=2, sort_keys=True))
            return EXIT_OK
        client.delete_webhook()
        print("webhook_deleted")
        return EXIT_OK
    except TelegramError as exc:
        print("telegram error: %s" % exc.category, file=sys.stderr)
        return EXIT_FAILURE


def _redact_info(info):
    if not isinstance(info, dict):
        return {"result": str(info)}
    safe = {}
    for key, value in info.items():
        if key in ("url", "last_error_message", "ip_address", "description"):
            safe[key] = value
        else:
            safe[key] = value
    return safe


if __name__ == "__main__":
    raise SystemExit(main())
