"""Pending clarification lifecycle."""

from __future__ import annotations

import json
import sqlite3

from app.db import transaction

STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_RESOLVED = "resolved"
STATUS_EXPIRED = "expired"
STATUS_UNABLE_TO_DELIVER = "unable_to_deliver"


def create(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    employee_telegram_user_id: int,
    question_text: str,
    missing_fields: list[str],
    prompt_source_message_id: int | None,
    round_number: int,
    now_iso: str,
) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO clarifications "
            "(event_id, employee_telegram_user_id, question_text, missing_fields_json, "
            " status, prompt_source_message_id, round, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                employee_telegram_user_id,
                question_text,
                json.dumps(sorted(missing_fields), ensure_ascii=False),
                STATUS_OPEN,
                prompt_source_message_id,
                round_number,
                now_iso,
            ),
        )
    return int(cursor.lastrowid)


def set_prompt_message(
    conn: sqlite3.Connection, clarification_id: int, *, chat_id: int, message_id: int
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clarifications SET prompt_chat_id = ?, prompt_message_id = ? "
            "WHERE id = ?",
            (chat_id, message_id, clarification_id),
        )


def mark_unable_to_deliver(
    conn: sqlite3.Connection, clarification_id: int, *, category: str, now_iso: str
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clarifications SET status = ?, last_error_category = ?, "
            "  resolved_at_utc = ? WHERE id = ?",
            (STATUS_UNABLE_TO_DELIVER, category, now_iso, clarification_id),
        )


def mark_send_error(
    conn: sqlite3.Connection, clarification_id: int, *, category: str
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clarifications SET last_error_category = ? WHERE id = ?",
            (category, clarification_id),
        )


def mark_answered(
    conn: sqlite3.Connection,
    clarification_id: int,
    *,
    response_source_message_id: int,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clarifications SET status = ?, response_source_message_id = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_ANSWERED, response_source_message_id, clarification_id, STATUS_OPEN),
        )


def mark_resolved(conn: sqlite3.Connection, clarification_id: int, *, now_iso: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clarifications SET status = ?, resolved_at_utc = ? WHERE id = ?",
            (STATUS_RESOLVED, now_iso, clarification_id),
        )


def close_open_for_event(
    conn: sqlite3.Connection, event_id: int, *, status: str, now_iso: str, category: str | None = None
) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE clarifications SET status = ?, resolved_at_utc = ?, "
            "  last_error_category = COALESCE(?, last_error_category) "
            "WHERE event_id = ? AND status IN (?, ?)",
            (status, now_iso, category, event_id, STATUS_OPEN, STATUS_ANSWERED),
        )
    return cursor.rowcount


def get(conn: sqlite3.Connection, clarification_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM clarifications WHERE id = ?", (clarification_id,)
    ).fetchone()


def latest_open_for_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM clarifications WHERE event_id = ? AND status = ? "
        "ORDER BY id DESC LIMIT 1",
        (event_id, STATUS_OPEN),
    ).fetchone()


def open_for_employee(conn: sqlite3.Connection, telegram_user_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM clarifications WHERE employee_telegram_user_id = ? "
            "AND status = ? ORDER BY id",
            (telegram_user_id, STATUS_OPEN),
        ).fetchall()
    )


def find_by_prompt_message(
    conn: sqlite3.Connection, *, chat_id: int, message_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM clarifications WHERE prompt_chat_id = ? AND prompt_message_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id, message_id),
    ).fetchone()


def missing_fields(row: sqlite3.Row) -> list[str]:
    try:
        value = json.loads(row["missing_fields_json"] or "[]")
    except ValueError:  # pragma: no cover - defensive
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []
