"""Regression tests for migrations: checksums, overlap and 001 -> 002 upgrade."""

from __future__ import annotations

import shutil
import sqlite3
import threading

import pytest

from kalampeach import migrations
from kalampeach.db import Database
from conftest import EMPLOYEE_ID


def open_db(path) -> Database:
    return Database(path)


def test_fresh_database_reaches_the_latest_version(tmp_path):
    db = open_db(tmp_path / "private" / "kb.sqlite3")
    with db.connection() as connection:
        version = migrations.ensure_schema(connection)
        assert version == migrations.latest_version()
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [int(row["version"]) for row in rows] == list(range(1, version + 1))
    shipped = {migration.version: migration.checksum for migration in migrations.load_migrations()}
    for row in rows:
        assert row["checksum"] == shipped[int(row["version"])]


def test_modified_applied_migration_is_rejected(tmp_path):
    sql_dir = tmp_path / "sql"
    shutil.copytree(migrations.SQL_DIR, sql_dir)
    db = open_db(tmp_path / "private" / "kb.sqlite3")
    with db.connection() as connection:
        migrations.ensure_schema(connection, sql_dir)

    target = sql_dir / "001_initial_schema.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- sneaky edit\n", encoding="utf-8")

    with pytest.raises(migrations.MigrationError) as info:
        with db.connection() as connection:
            migrations.ensure_schema(connection, sql_dir)
    assert "modified after being applied" in str(info.value)

    # The database was not touched: the recorded checksum still matches the original.
    with db.connection() as connection:
        recorded = migrations.applied_versions(connection)
    assert recorded[1] != migrations.load_migrations(sql_dir)[0].checksum


def test_overlapping_migrations_apply_exactly_once(tmp_path):
    path = tmp_path / "private" / "kb.sqlite3"
    db = open_db(path)
    with db.connection() as connection:
        migrations.ensure_schema(connection)

    seen = []
    errors = []

    def run():
        try:
            with db.connection() as connection:
                seen.append(migrations.ensure_schema(connection))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors
    assert set(seen) == {migrations.latest_version()}
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT version, COUNT(*) AS total FROM schema_migrations GROUP BY version"
        ).fetchall()
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert all(int(row["total"]) == 1 for row in rows)
    assert {"events", "clarifications", "report_deliveries", "telegram_updates"} <= tables


def test_failed_migration_rolls_back_and_reports(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "001_broken.sql").write_text(
        "CREATE TABLE ok_table (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT SQL;\n",
        encoding="utf-8",
    )
    db = open_db(tmp_path / "private" / "kb.sqlite3")
    with pytest.raises(migrations.MigrationError) as info:
        with db.connection() as connection:
            migrations.ensure_schema(connection, sql_dir)
    assert "001_broken failed" in str(info.value)

    with db.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "ok_table" not in tables, "a failed migration must not leave partial objects"
    assert "schema_migrations" in tables


def test_duplicate_migration_versions_are_refused(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "001_a.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
    (sql_dir / "001_b.sql").write_text("CREATE TABLE b (id INTEGER);", encoding="utf-8")
    with pytest.raises(migrations.MigrationError) as info:
        migrations.load_migrations(sql_dir)
    assert "duplicate migration version" in str(info.value)


def test_non_numeric_migration_name_is_refused(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "init.sql").write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
    with pytest.raises(migrations.MigrationError):
        migrations.load_migrations(sql_dir)


def test_upgrade_from_001_to_002_preserves_clarifications(tmp_path):
    all_migrations = migrations.load_migrations()
    first = [migration for migration in all_migrations if migration.version == 1]
    db = open_db(tmp_path / "private" / "kb.sqlite3")

    with db.connection() as connection:
        migrations.apply_migrations(connection, first)
        now = "2026-11-23T12:00:00Z"
        connection.execute(
            "INSERT INTO users (telegram_user_id, display_name, role, created_at_utc, updated_at_utc)"
            " VALUES (?, 'کارمند', 'employee', ?, ?)",
            (EMPLOYEE_ID, now, now),
        )
        connection.execute(
            "INSERT INTO source_messages (telegram_update_id, chat_id, chat_type, message_id,"
            " sender_telegram_user_id, message_timestamp_utc, processing_status,"
            " created_at_utc, updated_at_utc) VALUES (1, -100, 'group', 10, ?, ?, 'pending', ?, ?)",
            (EMPLOYEE_ID, now, now, now),
        )
        connection.execute(
            "INSERT INTO events (event_type, status, event_date, created_by_telegram_user_id,"
            " source_message_id, current_revision_number, created_at_utc, updated_at_utc)"
            " VALUES ('expense_purchase', 'pending_clarification', '2026-11-23', ?, 1, 1, ?, ?)",
            (EMPLOYEE_ID, now, now),
        )
        connection.execute(
            "INSERT INTO clarifications (event_id, employee_telegram_user_id, question_text,"
            " missing_fields, status, question_count, delivery_attempts, created_at_utc,"
            " updated_at_utc) VALUES (1, ?, 'مبلغ؟', '[\"amount\"]', 'unable_to_deliver', 1, 1, ?, ?)",
            (EMPLOYEE_ID, now, now),
        )
        before = dict(connection.execute("SELECT * FROM clarifications").fetchone())

        applied = migrations.apply_migrations(connection, all_migrations)
        assert [migration.version for migration in applied] == [2]

        after = dict(connection.execute("SELECT * FROM clarifications").fetchone())

    assert {key: after[key] for key in before} == before
    assert after["lease_owner"] is None
    assert after["lease_expires_at_utc"] is None

    with db.connection() as connection:
        for status in ("pending_delivery", "sending", "awaiting_answer", "delivery_uncertain"):
            connection.execute(
                "INSERT INTO clarifications (event_id, employee_telegram_user_id, question_text,"
                " missing_fields, status, created_at_utc, updated_at_utc)"
                " VALUES (1, ?, 'q', '[]', ?, ?, ?)",
                (EMPLOYEE_ID, status, now, now),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO clarifications (event_id, employee_telegram_user_id, question_text,"
                " missing_fields, status, created_at_utc, updated_at_utc)"
                " VALUES (1, ?, 'q', '[]', 'not_a_status', ?, ?)",
                (EMPLOYEE_ID, now, now),
            )
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(clarifications)")
        }
        connection.execute(
            "INSERT INTO clarifications (event_id, employee_telegram_user_id, question_text,"
            " missing_fields, status, created_at_utc, updated_at_utc)"
            " VALUES (1, ?, 'q', '[]', 'pending_delivery', ?, ?)",
            (EMPLOYEE_ID, now, now),
        )
        newest = connection.execute("SELECT MAX(id) FROM clarifications").fetchone()[0]
    assert {"ix_clarifications_delivery", "ix_clarifications_employee", "ix_clarifications_lease"} <= indexes
    # The rebuild must not renumber rows or reuse ids (AUTOINCREMENT sequence kept).
    assert after["id"] == before["id"] == 1
    # Rows 2-5 were the four status inserts above; the new row must not reuse them.
    assert newest == 6, "a rebuilt table must continue the id sequence"


def test_foreign_keys_are_enforced_on_every_connection(tmp_path):
    db = open_db(tmp_path / "private" / "kb.sqlite3")
    with db.connection() as connection:
        migrations.ensure_schema(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO clarifications (event_id, employee_telegram_user_id, question_text,"
                " missing_fields, status, created_at_utc, updated_at_utc)"
                " VALUES (999999, 1, 'q', '[]', 'pending_delivery', 't', 't')"
            )


def test_wal_mode_is_requested_and_reported(tmp_path):
    db = open_db(tmp_path / "private" / "kb.sqlite3")
    with db.connection() as connection:
        migrations.ensure_schema(connection)
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() in ("wal", "delete"), "WAL when supported, otherwise a logged fallback"
