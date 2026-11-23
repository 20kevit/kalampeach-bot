"""Schema initialisation, idempotent migrations and data preservation (G)."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import (
    REQUIRED_SCHEMA_VERSION,
    Database,
    integrity_check,
    migrate,
    pending_versions,
    require_current_schema,
    schema_version,
)
from app.errors import SchemaError
from tests.helpers import group_update, sale_event
from tests.helpers import make_config


def test_fresh_database_reaches_required_version(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.database_path)
    applied = db.ensure_schema()
    assert REQUIRED_SCHEMA_VERSION in applied
    assert db.version() == REQUIRED_SCHEMA_VERSION


def test_migrations_are_idempotent_and_preserve_data(ctx, fake_ai):
    from tests.helpers import process_once

    fake_ai.queue_extraction(sale_event())
    process_once(ctx, group_update(1, "فروش ۱۰۰ تومان"))

    with ctx.db.session() as conn:
        applied_again = migrate(conn)
        assert applied_again == []
        assert pending_versions(conn) == []
        require_current_schema(conn)
        assert integrity_check(conn) == "ok"
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0] == 1


def test_foreign_keys_are_enforced(ctx):
    with ctx.db.session() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (event_type, status, source_message_id, "
                "created_by_telegram_user_id, created_at_utc, updated_at_utc) "
                "VALUES ('sale_revenue', 'confirmed', 999999, 1, 'x', 'x')"
            )


def test_duplicate_source_message_is_impossible(ctx):
    from app.repo import messages

    with ctx.db.session() as conn:
        messages.get_or_create(
            conn,
            telegram_update_id=1,
            chat_id=-100,
            message_id=5,
            reply_to_message_id=None,
            sender_telegram_user_id=1,
            chat_type="supergroup",
            message_text="a",
            message_timestamp_utc="2025-12-23T10:00:00Z",
            processing_status="processed",
            now_iso="2025-12-23T10:00:00Z",
        )
        messages.get_or_create(
            conn,
            telegram_update_id=2,
            chat_id=-100,
            message_id=5,
            reply_to_message_id=None,
            sender_telegram_user_id=1,
            chat_type="supergroup",
            message_text="b",
            message_timestamp_utc="2025-12-23T10:00:00Z",
            processing_status="processed",
            now_iso="2025-12-23T10:00:00Z",
        )
        rows = conn.execute(
            "SELECT * FROM source_messages WHERE chat_id = -100 AND message_id = 5"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["message_text"] == "a"


def test_require_current_schema_fails_clearly_on_empty_database(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.database_path)
    db.connect(create_dir=True).close()
    with db.session() as conn:
        with pytest.raises(SchemaError) as excinfo:
            require_current_schema(conn)
    assert "init_db" in str(excinfo.value)


def test_schema_version_of_uninitialised_database_is_zero(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.database_path)
    conn = db.connect(create_dir=True)
    try:
        assert schema_version(conn) == 0
    finally:
        conn.close()
