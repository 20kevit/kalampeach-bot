"""Event projection and append-only revisions.

Every write here happens inside a single ``BEGIN IMMEDIATE`` transaction that
also writes the matching ``event_revisions`` row, so the current projection and
its history can never disagree.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from app.db import transaction
from app.domain.events import (
    CONFIRMED,
    PENDING_CLARIFICATION,
    REJECTED,
    fields_from_row,
    snapshot_json,
)


def create(
    conn: sqlite3.Connection,
    *,
    fields: Mapping[str, Any],
    details: Mapping[str, Any] | None,
    status: str,
    actor_telegram_user_id: int,
    source_message_id: int,
    now_iso: str,
) -> int:
    """Insert a new event plus revision #1."""

    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    confirmed_at = now_iso if status == CONFIRMED else None
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO events "
            "(event_type, status, event_date, amount, currency, item_name, quantity, "
            " unit, inventory_direction, description, notes, details_json, "
            " created_by_telegram_user_id, source_message_id, "
            " current_revision_number, confirmed_at_utc, created_at_utc, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                fields["event_type"],
                status,
                fields.get("event_date"),
                fields.get("amount"),
                fields.get("currency"),
                fields.get("item_name"),
                fields.get("quantity"),
                fields.get("unit"),
                fields.get("inventory_direction"),
                fields.get("description"),
                fields.get("notes"),
                details_json,
                actor_telegram_user_id,
                source_message_id,
                confirmed_at,
                now_iso,
                now_iso,
            ),
        )
        event_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO event_revisions "
            "(event_id, revision_number, actor_telegram_user_id, created_at_utc, "
            " source_message_id, clarification_id, reason, previous_snapshot_json, "
            " new_snapshot_json, validation_status) "
            "VALUES (?, 1, ?, ?, ?, NULL, ?, NULL, ?, ?)",
            (
                event_id,
                actor_telegram_user_id,
                now_iso,
                source_message_id,
                "initial_capture",
                snapshot_json(fields, status=status),
                "accepted" if status == CONFIRMED else status,
            ),
        )
    return event_id


def apply_revision(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    fields: Mapping[str, Any],
    status: str,
    details: Mapping[str, Any] | None,
    actor_telegram_user_id: int,
    source_message_id: int | None,
    reason: str,
    now_iso: str,
    clarification_id: int | None = None,
    validation_status: str = "accepted",
) -> int:
    """Append a revision and update the projection atomically.

    Returns the new revision number. Prior revisions are never modified.
    """

    with transaction(conn):
        current = conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if current is None:
            raise LookupError(f"event {event_id} does not exist")

        previous_fields = fields_from_row(current)
        new_revision = int(current["current_revision_number"]) + 1
        confirmed_at = current["confirmed_at_utc"]
        if status == CONFIRMED and not confirmed_at:
            confirmed_at = now_iso
        if status != CONFIRMED:
            confirmed_at = None

        conn.execute(
            "UPDATE events SET event_type = ?, status = ?, event_date = ?, amount = ?, "
            "  currency = ?, item_name = ?, quantity = ?, unit = ?, "
            "  inventory_direction = ?, description = ?, notes = ?, details_json = ?, "
            "  current_revision_number = ?, confirmed_at_utc = ?, updated_at_utc = ? "
            "WHERE id = ?",
            (
                fields["event_type"],
                status,
                fields.get("event_date"),
                fields.get("amount"),
                fields.get("currency"),
                fields.get("item_name"),
                fields.get("quantity"),
                fields.get("unit"),
                fields.get("inventory_direction"),
                fields.get("description"),
                fields.get("notes"),
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                new_revision,
                confirmed_at,
                now_iso,
                event_id,
            ),
        )
        conn.execute(
            "INSERT INTO event_revisions "
            "(event_id, revision_number, actor_telegram_user_id, created_at_utc, "
            " source_message_id, clarification_id, reason, previous_snapshot_json, "
            " new_snapshot_json, validation_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                new_revision,
                actor_telegram_user_id,
                now_iso,
                source_message_id,
                clarification_id,
                reason,
                snapshot_json(previous_fields, status=str(current["status"])),
                snapshot_json(fields, status=status),
                validation_status,
            ),
        )
    return new_revision


def get(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def get_by_source_message(conn: sqlite3.Connection, source_message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM events WHERE source_message_id = ?", (source_message_id,)
    ).fetchone()


def has_revision_from_source(conn: sqlite3.Connection, *, event_id: int, source_message_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM event_revisions WHERE event_id = ? AND source_message_id = ?",
        (event_id, source_message_id),
    ).fetchone()
    return row is not None


def confirmed_between(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
    confirmed_before_utc: str | None = None,
) -> list[sqlite3.Row]:
    """Confirmed events in an inclusive Tehran date range.

    ``confirmed_before_utc`` implements the daily report cutoff: an event
    confirmed after the cutoff belongs to a later report, which keeps a late
    clarification from retroactively changing a report already sent.
    """

    sql = (
        "SELECT * FROM events WHERE status = ? AND event_date IS NOT NULL "
        "AND event_date >= ? AND event_date <= ?"
    )
    params: list[Any] = [CONFIRMED, start_date, end_date]
    if confirmed_before_utc is not None:
        sql += " AND confirmed_at_utc IS NOT NULL AND confirmed_at_utc <= ?"
        params.append(confirmed_before_utc)
    sql += " ORDER BY event_date, id"
    return list(conn.execute(sql, params).fetchall())


def count_status_between(
    conn: sqlite3.Connection, *, start_date: str, end_date: str
) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM events "
        "WHERE event_date IS NOT NULL AND event_date >= ? AND event_date <= ? "
        "GROUP BY status",
        (start_date, end_date),
    ).fetchall()
    counts = {status: 0 for status in (CONFIRMED, PENDING_CLARIFICATION, REJECTED)}
    for row in rows:
        counts[str(row["status"])] = int(row["count"])
    return counts


def pending_without_date(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE status = ? AND event_date IS NULL",
        (PENDING_CLARIFICATION,),
    ).fetchone()
    return int(row["count"]) if row else 0
