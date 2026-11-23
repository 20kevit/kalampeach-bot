"""TEST-PLAN G: cPanel/Passenger entry point, storage safety, data integrity.

These tests are the executable part of the integrity checklist. They run offline
and never touch a live database, Telegram, NVIDIA, cPanel or the separate
accounting website.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kalampeach import migrations, repository
from kalampeach.timeutil import to_utc_iso
from conftest import BASE_ENV, EMPLOYEE_ID, GROUP_ID, REPO_ROOT
from helpers import seed_event

SCRIPT_ENV_KEYS = ("DATABASE_PATH", "LOG_LEVEL")


def script_env(tmp_path: Path, **overrides) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("KALAMPEACH_")}
    env.update(dict(BASE_ENV))
    env["DATABASE_PATH"] = str(tmp_path / "private" / "kalampeach_bot.sqlite3")
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def run_script(tmp_path: Path, *args, **overrides):
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(REPO_ROOT),
        env=script_env(tmp_path, **overrides),
        capture_output=True,
        text=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# G: entry point and offline runtime
# ---------------------------------------------------------------------------
WSGI_SMOKE = r"""
import io, json, os
from wsgiref.util import setup_testing_defaults

payload = json.loads(os.environ["SMOKE_REQUEST"])
os.environ.update(json.loads(os.environ["SMOKE_ENV"]))

import passenger_wsgi

environ = {"REQUEST_METHOD": payload["method"], "PATH_INFO": payload["path"]}
if payload.get("body") is not None:
    raw = payload["body"].encode("utf-8")
    environ["CONTENT_LENGTH"] = str(len(raw))
    environ["wsgi.input"] = io.BytesIO(raw)
    environ["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = payload.get("secret", "")
setup_testing_defaults(environ)

captured = {}

def start_response(status, headers, exc_info=None):
    captured["status"] = status
    captured["headers"] = dict(headers)
    return lambda data: None

body = b"".join(passenger_wsgi.application(environ, start_response))
print(json.dumps({"status": captured["status"], "body": body.decode("utf-8")}))
"""


def wsgi_request(tmp_path, *, method="GET", path="/health", body=None, secret=""):
    env = script_env(tmp_path)
    payload = {"method": method, "path": path, "body": body, "secret": secret}
    result = subprocess.run(
        [sys.executable, "-c", WSGI_SMOKE],
        cwd=str(REPO_ROOT),
        env={**env, "SMOKE_ENV": json.dumps(env), "SMOKE_REQUEST": json.dumps(payload)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_passenger_entry_point_serves_health(tmp_path):
    applied = run_script(tmp_path, "scripts/migrate.py", "--apply")
    assert applied.returncode == 0, applied.stderr

    response = wsgi_request(tmp_path)
    assert response["status"].startswith("200")
    document = json.loads(response["body"])
    assert document["status"] == "ok"
    assert document["database_ok"] is True
    assert document["schema_version"] == document["expected_schema_version"] == 1
    assert document["webhook_path"] == "/telegram/webhook"
    # The public database must never be reachable through the app root.
    assert not (REPO_ROOT / "public_html").exists()


def test_health_reports_degraded_before_migrations(tmp_path):
    response = wsgi_request(tmp_path)
    assert response["status"].startswith("503")
    document = json.loads(response["body"])
    assert document["status"] == "degraded"
    assert document["schema_version"] == 0
    assert document["expected_schema_version"] == 1


def test_passenger_entry_point_accepts_a_signed_update(tmp_path):
    applied = run_script(tmp_path, "scripts/migrate.py", "--apply")
    assert applied.returncode == 0, applied.stderr
    body = json.dumps(
        {
            "update_id": 1,
            "message": {
                "message_id": 5,
                "date": 1_795_000_000,
                "chat": {"id": GROUP_ID, "type": "group"},
                "from": {"id": EMPLOYEE_ID, "is_bot": False},
                "text": "فروش ۱۰۰ هزار تومان",
            },
        },
        ensure_ascii=False,
    )
    response = wsgi_request(
        tmp_path,
        method="POST",
        path="/telegram/webhook",
        body=body,
        secret=BASE_ENV["TELEGRAM_WEBHOOK_SECRET"],
    )
    assert response["status"].startswith("200")
    with sqlite3.connect(script_env(tmp_path)["DATABASE_PATH"]) as connection:
        queued = connection.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0]
    assert queued == 1, "the update is durably stored before the 200 response"


def test_no_long_polling_or_daemon_dependencies():
    forbidden_imports = {"celery", "redis", "rq", "dramatiq", "docker", "requests", "httpx", "aiohttp"}
    offenders = []
    for path in sorted((REPO_ROOT / "kalampeach").rglob("*.py")) + sorted(
        (REPO_ROOT / "scripts").glob("*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name in forbidden_imports:
                    offenders.append("%s imports %s" % (path.name, name))
    assert offenders == []

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "kalampeach").rglob("*.py")
    )
    assert "getUpdates" not in sources, "the bot must never long-poll Telegram"
    assert "while True" not in sources, "no always-on process loops"


def test_no_accounting_website_integration():
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in list((REPO_ROOT / "kalampeach").rglob("*.py"))
        + list((REPO_ROOT / "scripts").glob("*.py"))
    ).lower()
    markers = (
        "kalampeach" + ".com",
        "kalampeach" + ".ir",
        "accounting" + "_api",
        "accounting" + "-db",
        "mysql" + ".connector",
        "py" + "mysql",
        "psycopg",
        "kalampeach-accounting",
    )
    for marker in markers:
        assert marker not in production

    # Outbound hosts are the Telegram API and whatever the owner configures.
    config = (REPO_ROOT / "kalampeach" / "config.py").read_text(encoding="utf-8")
    assert "api.telegram.org" in config
    # No invented provider endpoint or model: both must be supplied by the owner.
    assert '_required_text(values, "NVIDIA_BASE_URL"' in config
    assert '_required_text(values, "NVIDIA_MODEL"' in config
    assert "integrate.api.nvidia.com" not in config
    assert "meta/llama" not in config.lower()


def test_cron_worker_script_exits_cleanly(tmp_path):
    result = run_script(tmp_path, "scripts/run_worker.py")
    assert result.returncode == 0, result.stderr
    assert "worker_run" in result.stdout or "claimed" in result.stdout


def test_worker_script_bounds_its_work(tmp_path):
    result = run_script(tmp_path, "scripts/run_worker.py", "--max-seconds", "1", "--batch-size", "5")
    assert result.returncode == 0, result.stderr


def test_migrate_status_script(tmp_path):
    result = run_script(tmp_path, "scripts/migrate.py", "--status")
    assert result.returncode == 0, result.stderr
    assert "001" in result.stdout
    assert "pending" in result.stdout.lower() or "not applied" in result.stdout.lower()


def test_migrate_apply_script_is_idempotent(tmp_path):
    first = run_script(tmp_path, "scripts/migrate.py", "--apply")
    assert first.returncode == 0, first.stderr
    second = run_script(tmp_path, "scripts/migrate.py", "--apply")
    assert second.returncode == 0, second.stderr
    with sqlite3.connect(script_env(tmp_path)["DATABASE_PATH"]) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
    assert versions == [1]


def test_doctor_script_reports_missing_configuration_without_secrets(tmp_path):
    result = run_script(
        tmp_path,
        "scripts/doctor.py",
        TELEGRAM_BOT_TOKEN=None,
        NVIDIA_API_KEY=None,
    )
    assert result.returncode != 0
    assert "TELEGRAM_BOT_TOKEN" in result.stdout
    assert BASE_ENV["NVIDIA_API_KEY"] not in result.stdout
    assert "nvapi-" not in result.stdout


def test_doctor_script_passes_with_complete_configuration(tmp_path):
    result = run_script(tmp_path, "scripts/doctor.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failure(s)" in result.stdout
    assert BASE_ENV["TELEGRAM_BOT_TOKEN"] not in result.stdout
    assert BASE_ENV["NVIDIA_API_KEY"] not in result.stdout


def test_backup_script_creates_a_verified_copy(tmp_path):
    applied = run_script(tmp_path, "scripts/migrate.py", "--apply")
    assert applied.returncode == 0, applied.stderr
    database = Path(script_env(tmp_path)["DATABASE_PATH"])

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (telegram_user_id, display_name, role, created_at_utc, updated_at_utc) "
            "VALUES (1111, 'کارمند', 'employee', '2026-11-23T00:00:00Z', '2026-11-23T00:00:00Z')"
        )
        connection.commit()

    backup_dir = tmp_path / "backups"
    result = run_script(tmp_path, "scripts/backup_db.py", "--output", str(backup_dir))
    assert result.returncode == 0, result.stderr

    copies = sorted(backup_dir.glob("kalampeach_bot_*.sqlite3"))
    assert len(copies) == 1
    assert oct(copies[0].stat().st_mode & 0o777) == "0o600"
    with sqlite3.connect(copies[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_backup_script_refuses_the_public_web_root(tmp_path):
    run_script(tmp_path, "scripts/migrate.py", "--apply")
    result = run_script(
        tmp_path, "scripts/backup_db.py", "--output", str(tmp_path / "public_html" / "backups")
    )
    assert result.returncode != 0
    assert "public" in result.stderr.lower()


def test_restore_procedure_is_documented_and_rehearsed(tmp_path):
    """A backup copy restores into a fresh path and keeps the schema/data."""
    run_script(tmp_path, "scripts/migrate.py", "--apply")
    database = Path(script_env(tmp_path)["DATABASE_PATH"])
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (telegram_user_id, display_name, role, created_at_utc, updated_at_utc) "
            "VALUES (1111, 'کارمند', 'employee', '2026-11-23T00:00:00Z', '2026-11-23T00:00:00Z')"
        )
        connection.commit()

    backup_dir = tmp_path / "backups"
    assert run_script(tmp_path, "scripts/backup_db.py", "--output", str(backup_dir)).returncode == 0
    copy = sorted(backup_dir.glob("*.sqlite3"))[0]

    restored = tmp_path / "restored" / "kalampeach_bot.sqlite3"
    restored.parent.mkdir(parents=True)
    restored.write_bytes(copy.read_bytes())

    status = run_script(tmp_path, "scripts/migrate.py", "--status", DATABASE_PATH=str(restored))
    assert status.returncode == 0, status.stderr
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    deployment = (REPO_ROOT / "CPANEL-DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "restore" in deployment.lower()


def test_declared_packages_match_the_repository():
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["tool"]["setuptools"]["packages"]
    for package in declared:
        directory = REPO_ROOT / package.replace(".", "/")
        assert directory.is_dir(), package
        assert (directory / "__init__.py").is_file(), "%s needs __init__.py" % package
    assert (REPO_ROOT / "passenger_wsgi.py").is_file()
    assert pyproject["project"]["dependencies"] == []


# ---------------------------------------------------------------------------
# G: schema and write integrity (in-process)
# ---------------------------------------------------------------------------
def test_migrations_preserve_existing_records(db):
    from kalampeach.migrations import ensure_schema, current_version

    with db.transaction() as connection:
        repository.upsert_user(
            connection,
            telegram_user_id=EMPLOYEE_ID,
            display_name="کارمند",
            role="employee",
            now="2026-11-23T12:00:00Z",
        )
        source, _ = repository.record_source_message(
            connection,
            telegram_update_id=1,
            chat_id=GROUP_ID,
            chat_type="group",
            message_id=10,
            reply_to_message_id=None,
            sender_telegram_user_id=EMPLOYEE_ID,
            message_text="فروش",
            message_timestamp_utc="2026-11-23T12:00:00Z",
            processing_status="processed",
            now="2026-11-23T12:00:00Z",
        )
        event_id = repository.insert_event(
            connection,
            event_type="sale_revenue",
            status="confirmed",
            event_date="2026-11-23",
            amount="1000",
            currency="تومان",
            currency_source="explicit",
            item_name=None,
            quantity=None,
            unit=None,
            inventory_direction=None,
            description=None,
            created_by_telegram_user_id=EMPLOYEE_ID,
            source_message_id=int(source["id"]),
            status_reason=None,
            now="2026-11-23T12:00:00Z",
        )
    assert int(event_id) == 1

    with db.connection() as connection:
        assert ensure_schema(connection) == current_version(connection) == 1
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_event_and_revision_write_is_transactional(db):
    from kalampeach.migrations import ensure_schema

    with db.connection() as connection:
        ensure_schema(connection)

    with db.transaction() as connection:
        repository.upsert_user(
            connection,
            telegram_user_id=EMPLOYEE_ID,
            display_name="کارمند",
            role="employee",
            now="2026-11-23T12:00:00Z",
        )
        source, _ = repository.record_source_message(
            connection,
            telegram_update_id=1,
            chat_id=GROUP_ID,
            chat_type="group",
            message_id=10,
            reply_to_message_id=None,
            sender_telegram_user_id=EMPLOYEE_ID,
            message_text="فروش",
            message_timestamp_utc="2026-11-23T12:00:00Z",
            processing_status="processed",
            now="2026-11-23T12:00:00Z",
        )
    source_id = int(source["id"])

    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            event_id = repository.insert_event(
                connection,
                event_type="sale_revenue",
                status="confirmed",
                event_date="2026-11-23",
                amount="1000",
                currency="تومان",
                currency_source="explicit",
                item_name=None,
                quantity=None,
                unit=None,
                inventory_direction=None,
                description=None,
                created_by_telegram_user_id=EMPLOYEE_ID,
                source_message_id=source_id,
                status_reason=None,
                now="2026-11-23T12:00:00Z",
            )
            repository.insert_revision(
                connection,
                event_id=int(event_id),
                revision_number=1,
                actor_telegram_user_id=EMPLOYEE_ID,
                change_type="created",
                change_reason=None,
                source_message_id=999999,  # violates the source_messages FK
                clarification_id=None,
                previous_snapshot=None,
                new_snapshot="{}",
                validation_status="accepted",
                now="2026-11-23T12:00:00Z",
            )

    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0] == 0


def test_money_and_quantities_are_stored_as_text(ctx):
    event_id = seed_event(ctx, 1, amount="0.30", quantity=None)
    with ctx.db.connection() as connection:
        row = connection.execute("SELECT amount FROM events WHERE id = ?", (int(event_id),)).fetchone()
        declared = {
            row_info[1]: row_info[2]
            for row_info in connection.execute("PRAGMA table_info(events)").fetchall()
        }
    assert isinstance(row["amount"], str)
    assert row["amount"] == "0.30"
    assert declared["amount"].upper().startswith("TEXT")


def test_timestamps_are_utc_and_dates_are_business_dates(ctx):
    seed_event(ctx, 1, amount="1000", event_date="2026-11-23")
    with ctx.db.connection() as connection:
        event = connection.execute("SELECT * FROM events").fetchone()
    assert event["event_date"] == "2026-11-23"
    assert event["created_at_utc"].endswith("Z")
    assert to_utc_iso(ctx.now()).endswith("Z")


def test_database_file_permissions_are_private(ctx):
    from kalampeach.migrations import ensure_schema

    with ctx.db.connection() as connection:
        ensure_schema(connection)
    mode = oct(ctx.settings.database_path.stat().st_mode & 0o777)
    assert mode in ("0o600", "0o640"), "the SQLite file must not be world-readable"


def test_database_path_must_stay_outside_the_public_root(env):
    from kalampeach.config import ConfigError, load_settings

    bad = dict(env)
    bad["DATABASE_PATH"] = "/home/example/public_html/kalampeach_bot.sqlite3"
    with pytest.raises(ConfigError) as info:
        load_settings(bad)
    assert any("public" in problem for problem in info.value.problems)


def test_repository_only_uses_parameterized_sql():
    source = (REPO_ROOT / "kalampeach" / "repository.py").read_text(encoding="utf-8")
    assert "execute(" in source
    # No f-string/percent SQL interpolation into execute calls.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("execute", "executemany") and node.args:
                first = node.args[0]
                assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                    "SQL must be a literal string, values passed as parameters"
                )


def test_no_secrets_or_databases_are_tracked(monkeypatch):
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60
    )
    assert tracked.returncode == 0
    files = tracked.stdout.split()
    assert ".env" not in files
    assert not [name for name in files if name.endswith((".sqlite3", ".sqlite", ".db"))]
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=" in example
    assert BASE_ENV["TELEGRAM_BOT_TOKEN"] not in example
    assert "nvapi-" not in example
