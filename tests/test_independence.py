"""Boundary checks: independence, no secrets, no daemons, no long polling.

These are static repository checks plus a WSGI-entry-point smoke test. They
encode the project rules from AGENTS.md/SCOPE.md so a future change cannot
quietly reintroduce a forbidden integration.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_FILES = sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
TEST_FILES = sorted((ROOT / "tests").glob("*.py"))
ALL_PY = APP_FILES + TEST_FILES + [ROOT / "passenger_wsgi.py"]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_long_polling_is_implemented():
    for path in APP_FILES + [ROOT / "passenger_wsgi.py"]:
        assert "getUpdates" not in _read(path), f"long polling referenced in {path}"


def test_no_daemon_or_external_queue_dependencies():
    forbidden = ("celery", "redis", "apscheduler", "systemd", "docker", "kubernetes", "rq")
    for path in ALL_PY:
        text = _read(path).lower()
        for token in forbidden:
            assert f"import {token}" not in text, f"{token} imported in {path}"
            assert f"from {token}" not in text, f"{token} imported in {path}"
    requirements = _read(ROOT / "requirements.txt").lower()
    for token in forbidden:
        assert token not in requirements


def test_no_connection_to_a_separate_accounting_system():
    suspicious = (
        r"accounting_api",
        r"accounting-api",
        r"kalampeach\.com",
        r"kalampeach\.ir",
        r"\berp\b",
        r"\bodoo\b",
        r"quickbooks",
    )
    for path in APP_FILES + [ROOT / "passenger_wsgi.py"]:
        text = _read(path).lower()
        for pattern in suspicious:
            assert not re.search(pattern, text), f"forbidden integration token {pattern!r} in {path}"


def test_only_telegram_and_configured_ai_hosts_are_contacted():
    hosts = set()
    for path in APP_FILES:
        for match in re.finditer(r"https?://([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", _read(path)):
            hosts.add(match.group(1))
    allowed = {"api.telegram.org"}
    allowed |= {host for host in hosts if host.endswith(".test")}
    assert hosts <= allowed, hosts


def test_no_committed_secrets_or_env_file():
    assert (ROOT / ".env.example").is_file()
    assert not (ROOT / ".env").exists()
    patterns = [
        re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token shape
        re.compile(r"\bnvapi-[A-Za-z0-9]{20,}\b"),  # NVIDIA key shape
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ]
    for path in ALL_PY + [ROOT / "README.md", ROOT / "CPANEL-DEPLOYMENT.md"]:
        text = _read(path)
        for pattern in patterns:
            assert not pattern.search(text), f"secret-like value in {path}"


def test_passenger_entry_point_exposes_a_wsgi_callable():
    import passenger_wsgi

    assert callable(passenger_wsgi.application)


def test_wsgi_entry_point_works_from_environment_mapping(tmp_path):
    from app.wsgi import build_application
    from tests.helpers import BASE_ENV, WEBHOOK_SECRET, group_update

    environ = dict(BASE_ENV)
    environ["DATABASE_PATH"] = str(tmp_path / "data" / "bot.sqlite3")
    application = build_application(environ)

    from app.db import Database

    Database(environ["DATABASE_PATH"]).ensure_schema()

    from werkzeug.test import Client

    client = Client(application)
    response = client.post(
        "/telegram/webhook",
        data=json.dumps(group_update(1, "سلام")),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_unconfigured_entry_point_returns_503_not_a_crash(tmp_path):
    from app.wsgi import build_application

    application = build_application({"LOG_LEVEL": "CRITICAL"})
    status_holder = {}

    def start_response(status, headers, exc_info=None):
        status_holder["status"] = status
        return lambda body: None

    body = b"".join(application({"REQUEST_METHOD": "POST", "PATH_INFO": "/telegram/webhook"}, start_response))
    assert status_holder["status"].startswith("503")
    assert b"configuration_incomplete" in body
    assert b"TELEGRAM_BOT_TOKEN" not in body


def test_webhook_module_does_not_import_the_ai_layer():
    text = _read(ROOT / "app" / "wsgi.py")
    assert "app.ai" not in text
    assert "NvidiaProvider" not in text


def test_secret_comparison_is_constant_time():
    text = _read(ROOT / "app" / "wsgi.py")
    assert "hmac.compare_digest" in text


def test_no_dynamic_code_execution_in_application_code():
    for path in APP_FILES:
        text = _read(path)
        assert "exec(" not in text, f"exec() in {path}"
        assert "eval(" not in text, f"eval() in {path}"
        assert "subprocess" not in text, f"subprocess in {path}"


def test_database_layer_uses_parameterised_queries():
    """A crude but useful guard: no f-string/format SQL with user values."""

    for path in sorted((ROOT / "app" / "repo").glob("*.py")):
        text = _read(path)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
                assert "%" not in stripped or "?" in stripped


def test_requirements_are_pinned():
    for line in _read(ROOT / "requirements.txt").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r"):
            continue
        assert "==" in line, f"unpinned dependency: {line}"


def test_sqlite_is_used_not_an_external_database():
    text = _read(ROOT / "app" / "db.py")
    assert "sqlite3" in text
    for token in ("psycopg2", "MySQLdb", "pymysql", "sqlalchemy"):
        assert token not in text
