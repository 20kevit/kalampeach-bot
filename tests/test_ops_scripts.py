"""Operational scripts: init, worker cron entry, diagnostics, backup/restore.

These run the real command lines, offline, against a temporary database. They do
not contact Telegram or NVIDIA.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from tests.helpers import BASE_ENV

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def write_env_file(tmp_path: Path, **overrides: str) -> Path:
    env = dict(BASE_ENV)
    env["DATABASE_PATH"] = str(tmp_path / "data" / "bot.sqlite3")
    env.update(overrides)
    lines = [f"{key}={value}" for key, value in env.items()]
    path = tmp_path / "private.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def run(script: str, *args: str, env_file: Path | None = None, extra_env: dict | None = None):
    command = [sys.executable, str(SCRIPTS / script), *args]
    if env_file is not None and "--env-file" not in args:
        command.extend(["--env-file", str(env_file)])
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONIOENCODING": "utf-8",
        "LOG_LEVEL": "CRITICAL",
    }
    environment.update(extra_env or {})
    return subprocess.run(
        command, capture_output=True, text=True, env=environment, cwd=str(ROOT), timeout=120
    )


def seed_database(env_file: Path, tmp_path: Path) -> None:
    """Create one confirmed event through the application code."""

    from app.db import Database
    from app.repo import events, messages
    from app.config import load_config

    environ = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        environ[key] = value
    config = load_config(environ)
    db = Database(config.database_path)
    db.ensure_schema()
    with db.session() as conn:
        source, _ = messages.get_or_create(
            conn,
            telegram_update_id=None,
            chat_id=config.telegram_group_id,
            message_id=1,
            reply_to_message_id=None,
            sender_telegram_user_id=900002,
            chat_type="supergroup",
            message_text="فروش ۱۰۰ تومان",
            message_timestamp_utc="2025-12-23T10:00:00Z",
            processing_status="processed",
            now_iso="2025-12-23T10:00:00Z",
        )
        events.create(
            conn,
            fields={
                "event_type": "sale_revenue",
                "event_date": "2025-12-23",
                "amount": "100",
                "currency": "تومان",
                "item_name": None,
                "quantity": None,
                "unit": None,
                "inventory_direction": None,
                "description": "seed",
                "notes": None,
            },
            details=None,
            status="confirmed",
            actor_telegram_user_id=900002,
            source_message_id=int(source["id"]),
            now_iso="2025-12-23T10:00:00Z",
        )


# ------------------------------------------------------------------- init_db
def test_init_db_creates_private_data_directory_and_schema(tmp_path):
    env_file = write_env_file(tmp_path)
    result = run("init_db.py", env_file=env_file)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] >= 1
    data_dir = tmp_path / "data"
    assert (data_dir / "bot.sqlite3").is_file()
    if os.name == "posix":
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


def test_init_db_is_idempotent(tmp_path):
    env_file = write_env_file(tmp_path)
    assert run("init_db.py", env_file=env_file).returncode == 0
    second = run("init_db.py", env_file=env_file)
    assert second.returncode == 0
    assert json.loads(second.stdout)["applied_versions"] == []


def test_init_db_fails_clearly_when_configuration_is_missing(tmp_path):
    env_file = tmp_path / "empty.env"
    env_file.write_text("LOG_LEVEL=CRITICAL\n", encoding="utf-8")
    result = run("init_db.py", env_file=env_file)
    assert result.returncode == 1
    assert "TELEGRAM_BOT_TOKEN" in result.stderr or "configuration" in result.stderr.lower()
    assert "nvapi-TEST-NOT-REAL" not in result.stdout + result.stderr


# ------------------------------------------------------------------ worker cli
def test_run_worker_cli_exits_cleanly_on_empty_queue(tmp_path):
    env_file = write_env_file(tmp_path)
    assert run("init_db.py", env_file=env_file).returncode == 0
    result = run("run_worker.py", "--no-reports", env_file=env_file)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["worker"]["claimed"] == 0


def test_run_worker_cli_refuses_uninitialised_database(tmp_path):
    env_file = write_env_file(tmp_path)
    result = run("run_worker.py", "--no-reports", env_file=env_file)
    assert result.returncode == 1
    assert "init_db" in result.stderr


# --------------------------------------------------------------- verify_setup
def test_verify_setup_reports_readiness_without_secrets(tmp_path):
    env_file = write_env_file(tmp_path)
    assert run("init_db.py", env_file=env_file).returncode == 0
    result = run("verify_setup.py", env_file=env_file)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["problems"] == []
    assert report["database"]["schema_version"] >= 1
    assert report["schedule_preview"]["tehran_now"]
    combined = result.stdout + result.stderr
    assert "TEST-TOKEN-NOT-REAL" not in combined
    assert "nvapi-TEST-NOT-REAL" not in combined
    assert "test-webhook-secret" not in combined


def test_verify_setup_exits_non_zero_on_missing_configuration(tmp_path):
    env_file = tmp_path / "empty.env"
    env_file.write_text("LOG_LEVEL=CRITICAL\n", encoding="utf-8")
    result = run("verify_setup.py", env_file=env_file)
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["problems"]


# ------------------------------------------------------------ backup / restore
def test_backup_verifies_and_restores_to_a_new_database(tmp_path):
    env_file = write_env_file(tmp_path)
    assert run("init_db.py", env_file=env_file).returncode == 0
    seed_database(env_file, tmp_path)

    backup_dir = tmp_path / "backups"
    result = run("backup_db.py", "--dest", str(backup_dir), env_file=env_file)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["integrity"] == "ok"
    assert payload["row_counts"]["events"] == 1
    assert payload["row_counts"]["event_revisions"] == 1
    backup_path = Path(payload["backup_path"])
    assert backup_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700

    # Restore drill: a copy of the backup must open, pass integrity_check and
    # still contain the audited event and its revision.
    restored = tmp_path / "restore" / "restored.sqlite3"
    restored.parent.mkdir()
    restored.write_bytes(backup_path.read_bytes())
    conn = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        event = conn.execute("SELECT * FROM events").fetchone()
        assert event[4] == "100"  # amount column
        assert conn.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_backup_refuses_public_directory(tmp_path):
    env_file = write_env_file(tmp_path)
    assert run("init_db.py", env_file=env_file).returncode == 0
    result = run(
        "backup_db.py", "--dest", str(tmp_path / "public_html" / "backups"), env_file=env_file
    )
    assert result.returncode == 1
    assert "public_html" in result.stderr


# ---------------------------------------------------------------- report admin
def test_report_admin_lists_deliveries(tmp_path):
    env_file = write_env_file(tmp_path)
    assert run("init_db.py", env_file=env_file).returncode == 0
    result = run("report_admin.py", "--list", "5", env_file=env_file)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
