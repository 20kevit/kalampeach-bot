#!/usr/bin/env python3
"""Read-only deployment diagnostics for the later cPanel verification phase.

Checks configuration validity, database presence/schema, timezone handling,
filesystem permissions and the report schedule preview. With ``--network`` it
also performs read-only Telegram/NVIDIA connectivity probes (no messages are
sent, no webhook is changed). Nothing in this script mutates state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from _common import build_parser

from app.config import load_config
from app.errors import ConfigError
from app.timeutil import local_datetime, to_utc_iso, utc_now


def _check_config(environ: dict) -> tuple[dict, list[str]]:
    try:
        config = load_config(environ)
    except ConfigError as exc:
        return {}, list(exc.problems)
    return (
        {
            "business_timezone": config.business_timezone,
            "daily_report_time": config.daily_report_time,
            "manager_id_configured": bool(config.manager_telegram_id),
            "employee_count": len(config.employee_telegram_ids),
            "group_id_configured": bool(config.telegram_group_id),
            "default_currency_configured": bool(config.default_currency),
            "database_path": config.database_path,
            "nvidia_model": config.nvidia_model,
            "nvidia_base_url": config.nvidia_base_url,
            "webhook_public_url": config.webhook_public_url,
        },
        [],
    )


def _check_database(config) -> dict:
    path = Path(config.database_path)
    result: dict = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        result["status"] = "missing (run scripts/init_db.py)"
        return result
    stat = path.stat()
    result["mode"] = oct(stat.st_mode & 0o777)
    result["bytes"] = stat.st_size
    result["parent_writable"] = os.access(path.parent, os.W_OK)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            result["sqlite_version"] = sqlite3.sqlite_version
            try:
                row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                result["schema_version"] = row[0] if row else 0
            except sqlite3.Error:
                result["schema_version"] = 0
            result["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["status"] = f"unreadable ({type(exc).__name__})"
    return result


def _schedule_preview(config, now: datetime) -> dict:
    from datetime import timedelta

    from app.services.reporting import due_daily_dates
    from app.timeutil import local_cutoff_utc

    local = local_datetime(now, config.timezone)
    hour, minute = config.report_hour_minute
    candidate_days = [local.date() + timedelta(days=offset) for offset in range(0, 3)]
    next_cutoff = next(
        (
            cutoff
            for cutoff in (local_cutoff_utc(day, config.timezone, hour, minute) for day in candidate_days)
            if cutoff > now
        ),
        None,
    )
    return {
        "utc_now": to_utc_iso(now),
        "tehran_now": local.isoformat(timespec="minutes"),
        "due_daily_dates": [day.isoformat() for day in due_daily_dates(now, config)],
        "next_cutoff_utc": to_utc_iso(next_cutoff) if next_cutoff else None,
    }


def _network_probe(config) -> dict:
    import requests

    result: dict = {}
    try:
        response = requests.get(
            f"{config.telegram_api_base_url}/bot{config.telegram_bot_token}/getMe",
            timeout=10,
        )
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        result["telegram"] = {
            "http_status": response.status_code,
            "ok": bool(body.get("ok")),
            "bot_username_present": bool((body.get("result") or {}).get("username")),
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        result["telegram"] = {"error": type(exc).__name__}

    try:
        response = requests.post(
            config.nvidia_base_url
            if config.nvidia_base_url.endswith("/chat/completions")
            else f"{config.nvidia_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {config.nvidia_api_key}"},
            json={"model": config.nvidia_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            timeout=20,
        )
        result["nvidia"] = {"http_status": response.status_code}
    except Exception as exc:  # noqa: BLE001
        result["nvidia"] = {"error": type(exc).__name__}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("Read-only configuration and environment diagnostics")
    parser.add_argument("--network", action="store_true", help="probe Telegram/NVIDIA (read-only)")
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    if args.env_file:
        environ["KALAMPEACH_ENV_FILE"] = args.env_file
        environ.pop("ENV_FILE", None)

    config_summary, problems = _check_config(environ)
    report: dict = {"config": config_summary, "problems": problems}

    if not problems:
        config = load_config(environ)
        now = utc_now()
        report["database"] = _check_database(config)
        report["schedule_preview"] = _schedule_preview(config, now)
        report["python_version"] = sys.version.split()[0]
        report["checked_at_utc"] = to_utc_iso(datetime.now(tz=timezone.utc))
        if args.network:
            report["network"] = _network_probe(config)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
