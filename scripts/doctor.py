#!/usr/bin/env python3
"""Read-only configuration and environment self-check.

Safe to run before deployment: it validates configuration, the SQLite module,
IANA timezone data, the database location/permissions, and migration status. It
never prints secret values, never calls Telegram or NVIDIA, and never migrates
or modifies data.

Exit codes: 0 = all checks passed (warnings allowed), 1 = at least one FAIL,
2 = configuration could not be loaded.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from _bootstrap import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK

from kalampeach import __version__
from kalampeach.config import ConfigError, load_settings
from kalampeach.db import Database
from kalampeach.migrations import MigrationError, describe, load_migrations
from kalampeach.timeutil import TimeError, load_timezone

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def add(self, status: str, label: str, detail: str = "") -> None:
        if status == FAIL:
            self.failures += 1
        elif status == WARN:
            self.warnings += 1
        line = "[%s] %s" % (status, label)
        if detail:
            line += " — " + detail
        print(line)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="KalamPeach configuration doctor.")
    parser.add_argument(
        "--env-file", default=None, help="optional private .env file to load (never public)"
    )
    return parser.parse_args(argv)


def check_static(report: Report) -> None:
    report.add(PASS, "python_version", "%d.%d.%d" % sys.version_info[:3])
    if sys.version_info < (3, 9):
        report.add(FAIL, "python_version", "Python 3.9 or newer is required")
    report.add(PASS, "sqlite_module", sqlite3.sqlite_version)
    try:
        load_timezone("Asia/Tehran")
        report.add(PASS, "tzdata", "Asia/Tehran available")
    except TimeError as exc:
        report.add(FAIL, "tzdata", str(exc))
    try:
        migrations = load_migrations()
        report.add(PASS, "migrations_present", "%d migration(s)" % len(migrations))
    except MigrationError as exc:
        report.add(FAIL, "migrations_present", str(exc))


def check_config(report: Report, env_file):
    import os

    env = None
    if env_file:
        env = dict(os.environ)
        env["ENV_FILE"] = env_file
    try:
        settings = load_settings(env)
    except ConfigError as exc:
        for problem in exc.problems:
            report.add(FAIL, "configuration", problem)
        return None

    report.add(PASS, "configuration", "all required keys present")
    report.add(PASS, "telegram_group_id_configured", _mask_id(settings.telegram_group_id))
    if settings.telegram_group_id >= 0:
        report.add(
            WARN,
            "telegram_group_id_sign",
            "group/supergroup chat IDs are normally negative; re-check the value",
        )
    report.add(PASS, "manager_configured", _mask_id(settings.manager_telegram_id))
    report.add(
        PASS,
        "recording_allowlist",
        "%d id(s); manager may record: %s"
        % (len(settings.employee_ids), "yes" if settings.can_record(settings.manager_telegram_id) else "no"),
    )
    report.add(PASS, "default_currency", settings.default_currency or "not set (unknown currency -> clarification)")
    report.add(PASS, "business_timezone", settings.business_timezone_name)
    report.add(PASS, "daily_report_time", settings.daily_report_time.strftime("%H:%M"))
    report.add(PASS, "webhook_url_path", settings.webhook_public_url)
    report.add(PASS, "ai_model_configured", settings.nvidia_model)
    if not settings.nvidia_base_url.startswith("https://"):
        report.add(WARN, "nvidia_base_url", "not https; verify with the provider")
    return settings


def check_storage(report: Report, settings) -> None:
    path = Path(settings.database_path)
    parent = path.parent
    if parent.exists():
        if parent.is_dir():
            report.add(PASS, "database_directory", str(parent))
            import os

            if not os.access(parent, os.W_OK):
                report.add(FAIL, "database_directory_writable", "not writable by this process")
            else:
                report.add(PASS, "database_directory_writable", "writable")
        else:
            report.add(FAIL, "database_directory", "path exists but is not a directory")
    else:
        report.add(WARN, "database_directory", "does not exist yet; it will be created 0700")

    if path.exists():
        if not path.is_file():
            report.add(FAIL, "database_file", "exists but is not a regular file")
            return
        db = Database(
            path,
            busy_timeout_ms=settings.sqlite_busy_timeout_ms,
            journal_mode=settings.sqlite_journal_mode,
            create_dirs=False,
        )
        health = db.health()
        if health.get("ok"):
            report.add(PASS, "database_open", "quick_check=%s" % health.get("integrity"))
        else:
            report.add(FAIL, "database_open", str(health.get("error", "unknown error")))
            return
        review = health.get("review_needed") or {}
        uncertain = int(review.get("clarifications_uncertain", 0)) + int(
            review.get("reports_uncertain", 0)
        )
        if uncertain:
            report.add(
                WARN,
                "delivery_review_needed",
                "%d item(s) with an unknown delivery outcome; see OPENCODE-HANDOFF.md"
                % uncertain,
            )
        else:
            report.add(PASS, "delivery_review_needed", "none")

        try:
            with db.connection() as connection:
                rows = describe(connection)
                pending = [row for row, applied in rows if not applied]
                if pending:
                    report.add(
                        WARN,
                        "migrations",
                        "%d pending (run scripts/migrate.py --apply)" % len(pending),
                    )
                else:
                    report.add(PASS, "migrations", "up to date")
        except Exception as exc:  # pragma: no cover - diagnostics only
            report.add(FAIL, "migrations", exc.__class__.__name__)
    else:
        report.add(WARN, "database_file", "not created yet (run scripts/migrate.py --apply)")


def _mask_id(value: int) -> str:
    text = str(value)
    if len(text) <= 4:
        return "****"
    return text[:2] + "***" + text[-2:]


def main(argv=None) -> int:
    args = parse_args(argv)
    report = Report()
    print("KalamPeach bot doctor — version %s" % __version__)
    print("This check is read-only: no webhook, cron, Telegram or NVIDIA call is made.")
    print("")
    check_static(report)
    settings = check_config(report, args.env_file)
    if settings is not None:
        check_storage(report, settings)
    print("")
    print("Result: %d failure(s), %d warning(s)" % (report.failures, report.warnings))
    if report.failures:
        return EXIT_FAILURE
    if settings is None:
        return EXIT_CONFIG
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
