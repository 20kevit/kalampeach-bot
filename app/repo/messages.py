"""Source message persistence.

``source_messages`` is the provenance record and the second idempotency layer:
``UNIQUE (chat_id, message_id)`` means a repeated update cannot create two
source rows, and ``events.source_message_id UNIQUE`` means it cannot create two
events either.
"""

from __future__ import annotations

import sqlite3

from app.db import transaction

STATUS_RECEIVED = "received"
STATUS_PROCESSED = "processed"
STATUS_IGNORED = "ignored"
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"
STATUS_CLARIFICATION = "clarification"
STATUS_UNCLEAR = "unclear"
STATUS_CORRECTION = "correction"


def get_or_create(
    conn: sqlite3.Connection,
    *,
    telegram_update_id: int | None,
    chat_id: int,
    message_id: int,
    reply_to_message_id: int | None,
    sender_telegram_user_id: int | None,
    chat_type: str,
    message_text: str | None,
    message_timestamp_utc: str | None,
    processing_status: str,
    now_iso: str,
) -> tuple[sqlite3.Row, bool]:
    """Insert the message once; return the stored row and whether it is new."""

    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO source_messages "
            "(telegram_update_id, chat_id, message_id, reply_to_message_id, "
            " sender_telegram_user_id, chat_type, message_text, "
            " message_timestamp_utc, processing_status, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, message_id) DO NOTHING",
            (
                telegram_update_id,
                chat_id,
                message_id,
                reply_to_message_id,
                sender_telegram_user_id,
                chat_type,
                message_text,
                message_timestamp_utc,
                processing_status,
                now_iso,
            ),
        )
        created = cursor.rowcount == 1
        row = conn.execute(
            "SELECT * FROM source_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
    return row, created


def get(conn: sqlite3.Connection, source_message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM source_messages WHERE id = ?", (source_message_id,)
    ).fetchone()


def get_by_telegram_ref(
    conn: sqlite3.Connection, *, chat_id: int, message_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM source_messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()


def set_status(conn: sqlite3.Connection, source_message_id: int, status: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE source_messages SET processing_status = ? WHERE id = ?",
            (status, source_message_id),
        )
