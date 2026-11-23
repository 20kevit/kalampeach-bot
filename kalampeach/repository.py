"""SQL access layer (no business rules, no AI, no Telegram).

Every function takes an open :mod:`sqlite3` connection so callers can compose
several operations inside one transaction. Statements are always parameterized.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .timeutil import to_utc_iso

# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def upsert_user(
    connection: sqlite3.Connection,
    *,
    telegram_user_id: int,
    display_name: Optional[str],
    role: str,
    now: str,
    is_active: bool = True,
) -> None:
    connection.execute(
        """
        INSERT INTO users (telegram_user_id, display_name, role, is_active,
                           created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (telegram_user_id) DO UPDATE SET
            display_name = COALESCE(excluded.display_name, users.display_name),
            role = excluded.role,
            is_active = excluded.is_active,
            updated_at_utc = excluded.updated_at_utc
        """,
        (telegram_user_id, display_name, role, 1 if is_active else 0, now, now),
    )


def get_user(connection: sqlite3.Connection, telegram_user_id: int) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()


def display_name_for(connection: sqlite3.Connection, telegram_user_id: int) -> Optional[str]:
    row = connection.execute(
        "SELECT display_name FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    return row["display_name"] if row else None


# ---------------------------------------------------------------------------
# Durable inbox
# ---------------------------------------------------------------------------


def enqueue_update(
    connection: sqlite3.Connection,
    *,
    update_id: int,
    raw_payload: str,
    chat_id: Optional[int],
    chat_type: Optional[str],
    now: str,
) -> bool:
    """Durably store one Telegram update. Returns False for a duplicate."""

    cursor = connection.execute(
        """
        INSERT INTO telegram_updates (update_id, received_at_utc, raw_payload, chat_id,
                                      chat_type, status, attempts, available_at_utc,
                                      updated_at_utc)
        VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        ON CONFLICT (update_id) DO NOTHING
        """,
        (update_id, now, raw_payload, chat_id, chat_type, now, now),
    )
    return cursor.rowcount == 1


def get_update(connection: sqlite3.Connection, update_id: int) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM telegram_updates WHERE update_id = ?", (update_id,)
    ).fetchone()


def expire_exhausted_updates(
    connection: sqlite3.Connection, *, now: str, max_attempts: int
) -> int:
    """Mark leased-out updates that already used all attempts as failed."""

    cursor = connection.execute(
        """
        UPDATE telegram_updates
           SET status = 'failed',
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               last_error_category = COALESCE(last_error_category, 'lease_expired'),
               last_error_detail = COALESCE(last_error_detail, 'lease expired'),
               updated_at_utc = ?
         WHERE status = 'processing'
           AND attempts >= ?
           AND lease_expires_at_utc IS NOT NULL
           AND lease_expires_at_utc <= ?
        """,
        (now, max_attempts, now),
    )
    return cursor.rowcount


def claim_next_update(
    connection: sqlite3.Connection,
    *,
    now: str,
    lease_seconds: int,
    max_attempts: int,
    worker_id: str,
) -> Optional[sqlite3.Row]:
    """Atomically claim one due update.

    Must be called inside a ``BEGIN IMMEDIATE`` transaction; SQLite then
    serializes competing cron invocations so one update is claimed once.
    """

    candidate = connection.execute(
        """
        SELECT update_id FROM telegram_updates
         WHERE attempts < ?
           AND (
                (status = 'queued' AND available_at_utc <= ?)
             OR (status = 'processing'
                 AND lease_expires_at_utc IS NOT NULL
                 AND lease_expires_at_utc <= ?)
               )
         ORDER BY update_id
         LIMIT 1
        """,
        (max_attempts, now, now),
    ).fetchone()
    if candidate is None:
        return None

    update_id = int(candidate["update_id"])
    lease_expiry = _shift_iso(now, seconds=lease_seconds)
    connection.execute(
        """
        UPDATE telegram_updates
           SET status = 'processing',
               attempts = attempts + 1,
               lease_owner = ?,
               lease_expires_at_utc = ?,
               updated_at_utc = ?
         WHERE update_id = ?
        """,
        (worker_id, lease_expiry, now, update_id),
    )
    return connection.execute(
        "SELECT * FROM telegram_updates WHERE update_id = ?", (update_id,)
    ).fetchone()


def finish_update(
    connection: sqlite3.Connection,
    *,
    update_id: int,
    status: str,
    now: str,
    error_category: Optional[str] = None,
    error_detail: Optional[str] = None,
    worker_id: Optional[str] = None,
) -> int:
    """Finalize an update. Returns the number of rows actually changed.

    When ``worker_id`` is supplied the write only applies while this worker still
    owns the lease. A worker whose lease was taken over by another cron run — for
    example because the host froze it mid-call — must not overwrite the state of
    the run that took over. Ownership rather than lease freshness is the guard, so
    a slow-but-uncontested worker may still finish normally.
    """

    if status not in ("done", "failed", "queued"):
        raise ValueError("unsupported update status: %s" % status)
    cursor = connection.execute(
        """
        UPDATE telegram_updates
           SET status = ?,
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               last_error_category = COALESCE(?, last_error_category),
               last_error_detail = COALESCE(?, last_error_detail),
               completed_at_utc = ?,
               updated_at_utc = ?
         WHERE update_id = ?
           AND (? IS NULL OR (lease_owner = ? AND status = 'processing'))
        """,
        (
            status,
            error_category,
            (error_detail or "")[:500] or None,
            now if status in ("done", "failed") else None,
            now,
            update_id,
            worker_id,
            worker_id,
        ),
    )
    return int(cursor.rowcount or 0)


def schedule_update_retry(
    connection: sqlite3.Connection,
    *,
    update_id: int,
    available_at: str,
    now: str,
    error_category: str,
    error_detail: Optional[str] = None,
    worker_id: Optional[str] = None,
) -> int:
    """Requeue an update for a later attempt. Returns rows changed.

    ``worker_id`` adds the same lease-owner guard as :func:`finish_update`.
    """

    cursor = connection.execute(
        """
        UPDATE telegram_updates
           SET status = 'queued',
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               available_at_utc = ?,
               last_error_category = ?,
               last_error_detail = ?,
               updated_at_utc = ?
         WHERE update_id = ?
           AND (? IS NULL OR (lease_owner = ? AND status = 'processing'))
        """,
        (
            available_at,
            error_category,
            (error_detail or "")[:500] or None,
            now,
            update_id,
            worker_id,
            worker_id,
        ),
    )
    return int(cursor.rowcount or 0)


def count_source_messages(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) AS total FROM source_messages").fetchone()
    return int(row["total"]) if row else 0


def count_source_messages_before(connection: sqlite3.Connection, moment_utc: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM source_messages WHERE message_timestamp_utc < ?",
        (moment_utc,),
    ).fetchone()
    return int(row["total"]) if row else 0


def count_updates_by_status(connection: sqlite3.Connection) -> Dict[str, int]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS total FROM telegram_updates GROUP BY status"
    ).fetchall()
    return {str(row["status"]): int(row["total"]) for row in rows}


# ---------------------------------------------------------------------------
# Source messages
# ---------------------------------------------------------------------------


def record_source_message(
    connection: sqlite3.Connection,
    *,
    telegram_update_id: Optional[int],
    chat_id: int,
    chat_type: str,
    message_id: int,
    reply_to_message_id: Optional[int],
    sender_telegram_user_id: Optional[int],
    message_text: str,
    message_timestamp_utc: str,
    processing_status: str,
    now: str,
) -> Tuple[sqlite3.Row, bool]:
    """Insert a source message idempotently; return ``(row, created)``."""

    cursor = connection.execute(
        """
        INSERT INTO source_messages (telegram_update_id, chat_id, chat_type, message_id,
                                     reply_to_message_id, sender_telegram_user_id,
                                     message_text, message_timestamp_utc, processing_status,
                                     created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (chat_id, message_id) DO NOTHING
        """,
        (
            telegram_update_id,
            chat_id,
            chat_type,
            message_id,
            reply_to_message_id,
            sender_telegram_user_id,
            message_text,
            message_timestamp_utc,
            processing_status,
            now,
            now,
        ),
    )
    created = cursor.rowcount == 1
    row = get_source_message_by_telegram(connection, chat_id, message_id)
    assert row is not None  # the insert or the pre-existing row must exist
    return row, created


def get_source_message_by_telegram(
    connection: sqlite3.Connection, chat_id: int, message_id: int
) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM source_messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()


def get_source_message(connection: sqlite3.Connection, source_message_id: int) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM source_messages WHERE id = ?", (source_message_id,)
    ).fetchone()


def set_source_status(
    connection: sqlite3.Connection,
    *,
    source_message_id: int,
    processing_status: str,
    now: str,
    failure_reason: Optional[str] = None,
) -> None:
    connection.execute(
        """
        UPDATE source_messages
           SET processing_status = ?, failure_reason = ?, updated_at_utc = ?
         WHERE id = ?
        """,
        (processing_status, (failure_reason or "")[:200] or None, now, source_message_id),
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

EVENT_FIELDS = (
    "event_type",
    "status",
    "event_date",
    "amount",
    "currency",
    "currency_source",
    "item_name",
    "quantity",
    "unit",
    "inventory_direction",
    "description",
)


def insert_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    status: str,
    event_date: str,
    amount: Optional[str],
    currency: Optional[str],
    currency_source: Optional[str],
    item_name: Optional[str],
    quantity: Optional[str],
    unit: Optional[str],
    inventory_direction: Optional[str],
    description: Optional[str],
    created_by_telegram_user_id: int,
    source_message_id: int,
    status_reason: Optional[str],
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO events (event_type, status, event_date, amount, currency, currency_source,
                            item_name, quantity, unit, inventory_direction, description,
                            created_by_telegram_user_id, source_message_id,
                            current_revision_number, status_reason, created_at_utc,
                            updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            event_type,
            status,
            event_date,
            amount,
            currency,
            currency_source,
            item_name,
            quantity,
            unit,
            inventory_direction,
            description,
            created_by_telegram_user_id,
            source_message_id,
            status_reason,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def get_event(connection: sqlite3.Connection, event_id: int) -> Optional[sqlite3.Row]:
    return connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def find_event_by_source_message(
    connection: sqlite3.Connection, source_message_id: int
) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM events WHERE source_message_id = ?", (source_message_id,)
    ).fetchone()


def find_events_by_revision_source(
    connection: sqlite3.Connection, source_message_id: int
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT DISTINCT e.*
          FROM events e
          JOIN event_revisions r ON r.event_id = e.id
         WHERE r.source_message_id = ?
         ORDER BY e.id
        """,
        (source_message_id,),
    ).fetchall()


def update_event_fields(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    event_type: str,
    status: str,
    event_date: str,
    amount: Optional[str],
    currency: Optional[str],
    currency_source: Optional[str],
    item_name: Optional[str],
    quantity: Optional[str],
    unit: Optional[str],
    inventory_direction: Optional[str],
    description: Optional[str],
    revision_number: int,
    status_reason: Optional[str],
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE events
           SET event_type = ?, status = ?, event_date = ?, amount = ?, currency = ?,
               currency_source = ?, item_name = ?, quantity = ?, unit = ?,
               inventory_direction = ?, description = ?, current_revision_number = ?,
               status_reason = ?, updated_at_utc = ?
         WHERE id = ?
        """,
        (
            event_type,
            status,
            event_date,
            amount,
            currency,
            currency_source,
            item_name,
            quantity,
            unit,
            inventory_direction,
            description,
            revision_number,
            status_reason,
            now,
            event_id,
        ),
    )


def list_events_for_period(
    connection: sqlite3.Connection, *, start_date: str, end_date: str
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT e.*, u.display_name AS author_display_name
          FROM events e
          LEFT JOIN users u ON u.telegram_user_id = e.created_by_telegram_user_id
         WHERE e.event_date BETWEEN ? AND ?
         ORDER BY e.event_date, e.id
        """,
        (start_date, end_date),
    ).fetchall()


def count_events_by_status_for_period(
    connection: sqlite3.Connection, *, start_date: str, end_date: str
) -> Dict[str, int]:
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS total
          FROM events
         WHERE event_date BETWEEN ? AND ?
         GROUP BY status
        """,
        (start_date, end_date),
    ).fetchall()
    return {str(row["status"]): int(row["total"]) for row in rows}


def count_events_total(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) AS total FROM events").fetchone()
    return int(row["total"]) if row else 0


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


def insert_revision(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    revision_number: int,
    actor_telegram_user_id: int,
    change_type: str,
    change_reason: Optional[str],
    source_message_id: Optional[int],
    clarification_id: Optional[int],
    previous_snapshot: Optional[str],
    new_snapshot: str,
    validation_status: str,
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO event_revisions (event_id, revision_number, actor_telegram_user_id,
                                     change_type, change_reason, source_message_id,
                                     clarification_id, previous_snapshot, new_snapshot,
                                     validation_status, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            revision_number,
            actor_telegram_user_id,
            change_type,
            change_reason,
            source_message_id,
            clarification_id,
            previous_snapshot,
            new_snapshot,
            validation_status,
            now,
        ),
    )
    return int(cursor.lastrowid)


def next_revision_number(connection: sqlite3.Connection, event_id: int) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(revision_number), 0) AS highest FROM event_revisions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return int(row["highest"]) + 1


def list_revisions(connection: sqlite3.Connection, event_id: int) -> List[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY revision_number",
        (event_id,),
    ).fetchall()


def get_latest_revision(connection: sqlite3.Connection, event_id: int) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM event_revisions
         WHERE event_id = ?
         ORDER BY revision_number DESC
         LIMIT 1
        """,
        (event_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Clarifications
# ---------------------------------------------------------------------------


def insert_clarification(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    employee_telegram_user_id: int,
    question_text: str,
    missing_fields: Sequence[str],
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO clarifications (event_id, employee_telegram_user_id, question_text,
                                    missing_fields, status, question_count, delivery_attempts,
                                    next_delivery_attempt_at_utc, created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, 'pending_delivery', 0, 0, ?, ?, ?)
        """,
        (
            event_id,
            employee_telegram_user_id,
            question_text,
            json.dumps(list(missing_fields)),
            now,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def get_clarification(connection: sqlite3.Connection, clarification_id: int) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM clarifications WHERE id = ?", (clarification_id,)
    ).fetchone()


def list_clarifications_for_delivery(
    connection: sqlite3.Connection, *, now: str, limit: int = 5
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM clarifications
         WHERE status = 'pending_delivery'
           AND (next_delivery_attempt_at_utc IS NULL OR next_delivery_attempt_at_utc <= ?)
         ORDER BY id
         LIMIT ?
        """,
        (now, limit),
    ).fetchall()


# Clarification delivery states
# ---------------------------
# pending_delivery -> sending (claim) -> awaiting_answer  (definitely delivered)
#                                     -> pending_delivery (retryable, not sent)
#                                     -> unable_to_deliver (permanent rejection)
#                                     -> delivery_uncertain (outcome unknown)
# A row left in ``sending`` past its lease is swept to ``delivery_uncertain``.
CLARIFICATION_OPEN_STATES = ("pending_delivery", "sending", "awaiting_answer", "delivery_uncertain")


def claim_clarification_for_delivery(
    connection: sqlite3.Connection,
    *,
    clarification_id: int,
    now: str,
    lease_seconds: int,
    worker_id: str,
) -> bool:
    """Claim one due question before calling Telegram.

    The claim is the durable record that a send is about to happen, so a process
    killed mid-call cannot silently leave the row looking "not yet attempted".
    """

    lease_expiry = _shift_iso(now, seconds=lease_seconds)
    cursor = connection.execute(
        """
        UPDATE clarifications
           SET status = 'sending',
               delivery_attempts = delivery_attempts + 1,
               lease_owner = ?,
               lease_expires_at_utc = ?,
               updated_at_utc = ?
         WHERE id = ?
           AND status = 'pending_delivery'
           AND (next_delivery_attempt_at_utc IS NULL OR next_delivery_attempt_at_utc <= ?)
        """,
        (worker_id, lease_expiry, now, clarification_id, now),
    )
    return int(cursor.rowcount or 0) == 1


def mark_clarification_delivered(
    connection: sqlite3.Connection,
    *,
    clarification_id: int,
    now: str,
    question_count: int,
    worker_id: Optional[str] = None,
) -> int:
    """Record a confirmed send. Returns rows changed (0 means the claim was lost)."""

    cursor = connection.execute(
        """
        UPDATE clarifications
           SET status = 'awaiting_answer',
               question_count = ?,
               next_delivery_attempt_at_utc = NULL,
               last_delivery_error = NULL,
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               updated_at_utc = ?
         WHERE id = ?
           AND (? IS NULL OR (status = 'sending' AND lease_owner = ?))
        """,
        (question_count, now, clarification_id, worker_id, worker_id),
    )
    return int(cursor.rowcount or 0)


def release_clarification_for_retry(
    connection: sqlite3.Connection,
    *,
    clarification_id: int,
    error_category: str,
    next_attempt_at: str,
    now: str,
    worker_id: Optional[str] = None,
) -> int:
    """Return a question to ``pending_delivery`` after a definitely-unsent attempt."""

    cursor = connection.execute(
        """
        UPDATE clarifications
           SET status = 'pending_delivery',
               last_delivery_error = ?,
               next_delivery_attempt_at_utc = ?,
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               updated_at_utc = ?
         WHERE id = ?
           AND (? IS NULL OR lease_owner = ?)
        """,
        (error_category, next_attempt_at, now, clarification_id, worker_id, worker_id),
    )
    return int(cursor.rowcount or 0)


def mark_clarification_undeliverable(
    connection: sqlite3.Connection,
    *,
    clarification_id: int,
    error_category: str,
    now: str,
    worker_id: Optional[str] = None,
) -> int:
    """Permanent rejection (blocked bot, chat not found, ...). Never retried."""

    cursor = connection.execute(
        """
        UPDATE clarifications
           SET status = 'unable_to_deliver',
               last_delivery_error = ?,
               next_delivery_attempt_at_utc = NULL,
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               updated_at_utc = ?
         WHERE id = ?
           AND (? IS NULL OR lease_owner = ?)
        """,
        (error_category, now, clarification_id, worker_id, worker_id),
    )
    return int(cursor.rowcount or 0)


def mark_clarification_delivery_uncertain(
    connection: sqlite3.Connection,
    *,
    clarification_id: int,
    error_category: str,
    now: str,
    worker_id: Optional[str] = None,
) -> int:
    """The question may or may not have arrived. Never resend automatically."""

    cursor = connection.execute(
        """
        UPDATE clarifications
           SET status = 'delivery_uncertain',
               last_delivery_error = ?,
               next_delivery_attempt_at_utc = NULL,
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               updated_at_utc = ?
         WHERE id = ?
           AND (? IS NULL OR lease_owner = ?)
        """,
        (error_category, now, clarification_id, worker_id, worker_id),
    )
    return int(cursor.rowcount or 0)


def expire_stale_clarification_deliveries(connection: sqlite3.Connection, *, now: str) -> List[int]:
    """Sweep questions whose sending worker died before recording the outcome.

    Returns the affected ids so the caller can log them for operator review.
    """

    rows = connection.execute(
        """
        SELECT id FROM clarifications
         WHERE status = 'sending'
           AND lease_expires_at_utc IS NOT NULL
           AND lease_expires_at_utc <= ?
        """,
        (now,),
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    for clarification_id in ids:
        connection.execute(
            """
            UPDATE clarifications
               SET status = 'delivery_uncertain',
                   last_delivery_error = 'lease_expired_during_send',
                   next_delivery_attempt_at_utc = NULL,
                   lease_owner = NULL,
                   lease_expires_at_utc = NULL,
                   updated_at_utc = ?
             WHERE id = ? AND status = 'sending'
            """,
            (now, clarification_id),
        )
    return ids


def mark_clarification_answered(
    connection: sqlite3.Connection,
    *,
    clarification_id: int,
    response_source_message_id: int,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE clarifications
           SET status = 'answered',
               response_source_message_id = ?,
               answered_at_utc = ?,
               updated_at_utc = ?
         WHERE id = ?
        """,
        (response_source_message_id, now, now, clarification_id),
    )


def mark_clarification_resolved(
    connection: sqlite3.Connection, *, clarification_id: int, now: str
) -> None:
    connection.execute(
        """
        UPDATE clarifications
           SET status = 'resolved',
               resolved_at_utc = ?,
               next_delivery_attempt_at_utc = NULL,
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               updated_at_utc = ?
         WHERE id = ?
        """,
        (now, now, clarification_id),
    )


def expire_clarification(connection: sqlite3.Connection, *, clarification_id: int, now: str) -> None:
    connection.execute(
        """
        UPDATE clarifications
           SET status = 'expired', next_delivery_attempt_at_utc = NULL, updated_at_utc = ?
         WHERE id = ?
        """,
        (now, clarification_id),
    )


def requeue_clarification_question(
    connection: sqlite3.Connection,
    *,
    clarification_id: int,
    question_text: str,
    missing_fields: Sequence[str],
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE clarifications
           SET status = 'pending_delivery',
               question_text = ?,
               missing_fields = ?,
               next_delivery_attempt_at_utc = ?,
               lease_owner = NULL,
               lease_expires_at_utc = NULL,
               updated_at_utc = ?
         WHERE id = ?
        """,
        (question_text, json.dumps(list(missing_fields)), now, now, clarification_id),
    )


def list_open_clarifications(
    connection: sqlite3.Connection, employee_telegram_user_id: int
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM clarifications
         WHERE employee_telegram_user_id = ?
           AND status IN ('pending_delivery', 'sending', 'awaiting_answer', 'delivery_uncertain')
         ORDER BY id
        """,
        (employee_telegram_user_id,),
    ).fetchall()


def count_clarifications_in_status(connection: sqlite3.Connection, status: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM clarifications WHERE status = ?", (status,)
    ).fetchone()
    return int(row["total"]) if row else 0


def count_open_clarifications(connection: sqlite3.Connection) -> int:
    """Questions that still need a human: queued, being sent, asked or uncertain."""

    row = connection.execute(
        "SELECT COUNT(*) AS total FROM clarifications WHERE status IN"
        " ('pending_delivery', 'sending', 'awaiting_answer', 'delivery_uncertain')"
    ).fetchone()
    return int(row["total"]) if row else 0


# ---------------------------------------------------------------------------
# Bot messages (reply-target resolution)
# ---------------------------------------------------------------------------


def record_bot_message(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    message_id: int,
    kind: str,
    now: str,
    event_id: Optional[int] = None,
    clarification_id: Optional[int] = None,
    report_request_id: Optional[int] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO bot_messages (chat_id, message_id, kind, event_id, clarification_id,
                                  report_request_id, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (chat_id, message_id) DO NOTHING
        """,
        (chat_id, message_id, kind, event_id, clarification_id, report_request_id, now),
    )


def find_bot_message(
    connection: sqlite3.Connection, chat_id: int, message_id: int
) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM bot_messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# Report deliveries
# ---------------------------------------------------------------------------


def ensure_report_delivery(
    connection: sqlite3.Connection,
    *,
    report_key: str,
    report_type: str,
    manager_telegram_user_id: int,
    period_start_date: str,
    period_end_date: str,
    scheduled_at_utc: str,
    now: str,
    variant: str = "summary",
    status: str = "pending",
) -> Tuple[sqlite3.Row, bool]:
    """Create the delivery row once. Returns ``(row, created)``."""

    if status not in ("pending", "skipped"):
        raise ValueError("unsupported initial report delivery status: %s" % status)
    cursor = connection.execute(
        """
        INSERT INTO report_deliveries (report_key, report_type, variant,
                                       manager_telegram_user_id, period_start_date,
                                       period_end_date, scheduled_at_utc, status, attempts,
                                       next_attempt_at_utc, created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT (report_key) DO NOTHING
        """,
        (
            report_key,
            report_type,
            variant,
            manager_telegram_user_id,
            period_start_date,
            period_end_date,
            scheduled_at_utc,
            status,
            now if status == "pending" else None,
            now,
            now,
        ),
    )
    row = get_report_delivery_by_key(connection, report_key)
    assert row is not None
    return row, int(cursor.rowcount or 0) == 1


def get_report_delivery(connection: sqlite3.Connection, delivery_id: int) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM report_deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()


def get_report_delivery_by_key(
    connection: sqlite3.Connection, report_key: str
) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM report_deliveries WHERE report_key = ?", (report_key,)
    ).fetchone()


def claim_report_delivery(
    connection: sqlite3.Connection,
    *,
    delivery_id: int,
    now: str,
    lease_seconds: int,
    worker_id: str,
) -> bool:
    """Lease a delivery row; returns False when another worker holds it."""

    lease_expiry = _shift_iso(now, seconds=lease_seconds)
    cursor = connection.execute(
        """
        UPDATE report_deliveries
           SET status = 'sending',
               attempts = attempts + 1,
               lease_owner = ?,
               lease_expires_at_utc = ?,
               updated_at_utc = ?
         WHERE id = ?
           AND status IN ('pending', 'failed', 'sending')
           AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= ?)
           AND (lease_expires_at_utc IS NULL OR lease_expires_at_utc <= ?)
        """,
        (worker_id, lease_expiry, now, delivery_id, now, now),
    )
    return cursor.rowcount == 1


def release_report_delivery(
    connection: sqlite3.Connection,
    *,
    delivery_id: int,
    status: str,
    error_category: Optional[str],
    next_attempt_at: Optional[str],
    now: str,
) -> None:
    if status not in ("pending", "failed", "failed_uncertain", "skipped"):
        raise ValueError("unsupported report delivery status: %s" % status)
    connection.execute(
        """
        UPDATE report_deliveries
           SET status = ?, last_error_category = ?, next_attempt_at_utc = ?,
               lease_owner = NULL, lease_expires_at_utc = NULL, updated_at_utc = ?
         WHERE id = ?
        """,
        (status, error_category, next_attempt_at, now, delivery_id),
    )


def update_report_progress(
    connection: sqlite3.Connection,
    *,
    delivery_id: int,
    chunk_count: int,
    chunks_sent: int,
    telegram_message_ids: Sequence[int],
    snapshot_json: Optional[str],
    narrative_used: bool,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE report_deliveries
           SET chunk_count = ?, chunks_sent = ?, telegram_message_ids = ?,
               snapshot_json = COALESCE(?, snapshot_json), narrative_used = ?,
               updated_at_utc = ?
         WHERE id = ?
        """,
        (
            chunk_count,
            chunks_sent,
            json.dumps([int(value) for value in telegram_message_ids]),
            snapshot_json,
            1 if narrative_used else 0,
            now,
            delivery_id,
        ),
    )


def mark_report_sent(
    connection: sqlite3.Connection, *, delivery_id: int, now: str
) -> None:
    connection.execute(
        """
        UPDATE report_deliveries
           SET status = 'sent', sent_at_utc = ?, lease_owner = NULL,
               lease_expires_at_utc = NULL, next_attempt_at_utc = NULL,
               last_error_category = NULL, updated_at_utc = ?
         WHERE id = ?
        """,
        (now, now, delivery_id),
    )


def list_report_deliveries_due(
    connection: sqlite3.Connection, *, now: str, limit: int = 5
) -> List[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM report_deliveries
         WHERE status IN ('pending', 'failed', 'sending')
           AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= ?)
         ORDER BY id
         LIMIT ?
        """,
        (now, limit),
    ).fetchall()


# ---------------------------------------------------------------------------
# Report requests (manager on-demand reports)
# ---------------------------------------------------------------------------


def insert_report_request(
    connection: sqlite3.Connection,
    *,
    manager_telegram_user_id: int,
    request_text: str,
    now: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO report_requests (manager_telegram_user_id, request_text, status,
                                     created_at_utc, updated_at_utc)
        VALUES (?, ?, 'received', ?, ?)
        """,
        (manager_telegram_user_id, request_text, now, now),
    )
    return int(cursor.lastrowid)


def ensure_report_request(
    connection: sqlite3.Connection,
    *,
    source_message_id: int,
    manager_telegram_user_id: int,
    request_text: str,
    now: str,
) -> sqlite3.Row:
    """Idempotently create the request row for one manager message."""

    connection.execute(
        """
        INSERT INTO report_requests (source_message_id, manager_telegram_user_id, request_text,
                                     status, created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, 'received', ?, ?)
        ON CONFLICT (source_message_id) DO NOTHING
        """,
        (source_message_id, manager_telegram_user_id, request_text[:2000], now, now),
    )
    row = connection.execute(
        "SELECT * FROM report_requests WHERE source_message_id = ?", (source_message_id,)
    ).fetchone()
    assert row is not None
    return row


def count_report_prompts(connection: sqlite3.Connection, request_id: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS total FROM bot_messages WHERE report_request_id = ? AND kind = 'report_prompt'",
        (request_id,),
    ).fetchone()
    return int(row["total"]) if row else 0


def get_report_request(
    connection: sqlite3.Connection, request_id: int
) -> Optional[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM report_requests WHERE id = ?", (request_id,)
    ).fetchone()


def update_report_request(
    connection: sqlite3.Connection,
    *,
    request_id: int,
    status: str,
    now: str,
    period_kind: Optional[str] = None,
    report_type: Optional[str] = None,
    report_delivery_id: Optional[int] = None,
    resolved: bool = False,
) -> None:
    if status not in ("received", "awaiting_period", "resolved", "expired"):
        raise ValueError("unsupported report request status: %s" % status)
    connection.execute(
        """
        UPDATE report_requests
           SET status = ?,
               period_kind = COALESCE(?, period_kind),
               report_type = COALESCE(?, report_type),
               report_delivery_id = COALESCE(?, report_delivery_id),
               resolved_at_utc = CASE WHEN ? THEN ? ELSE resolved_at_utc END,
               updated_at_utc = ?
         WHERE id = ?
        """,
        (
            status,
            period_kind,
            report_type,
            report_delivery_id,
            1 if resolved else 0,
            now,
            now,
            request_id,
        ),
    )


def find_open_report_request(connection: sqlite3.Connection, manager_id: int) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM report_requests
         WHERE manager_telegram_user_id = ? AND status = 'awaiting_period'
         ORDER BY id DESC
         LIMIT 1
        """,
        (manager_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# AI audit
# ---------------------------------------------------------------------------


def record_ai_call(
    connection: sqlite3.Connection,
    *,
    operation: str,
    provider: str,
    model: Optional[str],
    outcome: str,
    latency_ms: Optional[int],
    now: str,
    error_category: Optional[str] = None,
    source_message_id: Optional[int] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO ai_calls (operation, provider, model, outcome, error_category,
                              latency_ms, source_message_id, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation,
            provider,
            model,
            outcome,
            error_category,
            latency_ms,
            source_message_id,
            now,
        ),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _shift_iso(iso_value: str, *, seconds: int) -> str:
    from datetime import timedelta

    from .timeutil import parse_utc_iso

    return to_utc_iso(parse_utc_iso(iso_value) + timedelta(seconds=seconds))


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]
