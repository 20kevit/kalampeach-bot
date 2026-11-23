"""Durable Telegram update inbox — the queue that replaces a daemon.

The webhook only inserts; the cron worker claims, processes and settles rows.
Claims are leases stored in the database, so overlapping cron invocations and
Passenger restarts are safe.
"""

from __future__ import annotations

import sqlite3

from app.db import transaction

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_IGNORED = "ignored"
STATUS_RETRY = "retry"
STATUS_FAILED = "failed"

CLAIMABLE_STATUSES = (STATUS_QUEUED, STATUS_RETRY)


def enqueue(
    conn: sqlite3.Connection,
    *,
    update_id: int,
    payload_json: str,
    kind: str | None,
    now_iso: str,
) -> tuple[bool, int]:
    """Persist one update. Returns ``(inserted, inbox_row_id)``.

    A duplicate ``update_id`` is a no-op that reports the existing row, which is
    what makes webhook retries safe.
    """

    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO telegram_inbox "
            "(update_id, received_at_utc, payload_json, kind, status, attempts, "
            " available_at_utc, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(update_id) DO NOTHING",
            (update_id, now_iso, payload_json, kind, STATUS_QUEUED, now_iso, now_iso),
        )
        inserted = cursor.rowcount == 1
        row = conn.execute(
            "SELECT id FROM telegram_inbox WHERE update_id = ?", (update_id,)
        ).fetchone()
    return inserted, int(row["id"])


def fail_exhausted_claims(
    conn: sqlite3.Connection, *, now_iso: str, max_attempts: int
) -> int:
    """Mark abandoned lease rows that already used every attempt as failed.

    Without this, a crash loop that always dies inside processing would keep a
    row claimable forever and never become observable.
    """

    with transaction(conn):
        cursor = conn.execute(
            "UPDATE telegram_inbox "
            "SET status = ?, last_error_category = 'CLAIM_EXPIRED', "
            "    claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ? "
            "WHERE status = ? AND attempts >= ? "
            "  AND (claimed_until_utc IS NULL OR claimed_until_utc <= ?)",
            (STATUS_FAILED, now_iso, STATUS_PROCESSING, max_attempts, now_iso),
        )
    return cursor.rowcount


def claim_batch(
    conn: sqlite3.Connection,
    *,
    token: str,
    now_iso: str,
    lease_until_iso: str,
    limit: int,
    max_attempts: int,
) -> list[sqlite3.Row]:
    """Atomically claim up to ``limit`` due rows for this worker invocation."""

    with transaction(conn):
        conn.execute(
            "UPDATE telegram_inbox "
            "SET status = ?, claim_token = ?, claimed_until_utc = ?, "
            "    attempts = attempts + 1, updated_at_utc = ? "
            "WHERE id IN ("
            "  SELECT id FROM telegram_inbox "
            "  WHERE attempts < ? AND ("
            "    (status IN (?, ?) AND available_at_utc <= ?) "
            "    OR (status = ? AND (claimed_until_utc IS NULL OR claimed_until_utc <= ?))"
            "  ) "
            "  ORDER BY id LIMIT ?"
            ")",
            (
                STATUS_PROCESSING,
                token,
                lease_until_iso,
                now_iso,
                max_attempts,
                STATUS_QUEUED,
                STATUS_RETRY,
                now_iso,
                STATUS_PROCESSING,
                now_iso,
                limit,
            ),
        )
        rows = conn.execute(
            "SELECT * FROM telegram_inbox WHERE claim_token = ? AND status = ? "
            "ORDER BY id",
            (token, STATUS_PROCESSING),
        ).fetchall()
    return list(rows)


def mark_done(
    conn: sqlite3.Connection,
    *,
    inbox_id: int,
    token: str,
    now_iso: str,
    error_category: str | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE telegram_inbox SET status = ?, processed_at_utc = ?, "
            "  claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ?, "
            "  last_error_category = COALESCE(?, last_error_category) "
            "WHERE id = ? AND claim_token = ?",
            (STATUS_DONE, now_iso, now_iso, error_category, inbox_id, token),
        )


def mark_ignored(
    conn: sqlite3.Connection,
    *,
    inbox_id: int,
    token: str,
    now_iso: str,
    reason: str,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE telegram_inbox SET status = ?, processed_at_utc = ?, "
            "  claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ?, "
            "  last_error_category = ? "
            "WHERE id = ? AND claim_token = ?",
            (STATUS_IGNORED, now_iso, now_iso, reason, inbox_id, token),
        )


def mark_retry(
    conn: sqlite3.Connection,
    *,
    inbox_id: int,
    token: str,
    now_iso: str,
    next_attempt_iso: str,
    error_category: str,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE telegram_inbox SET status = ?, available_at_utc = ?, "
            "  claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ?, "
            "  last_error_category = ? "
            "WHERE id = ? AND claim_token = ?",
            (STATUS_RETRY, next_attempt_iso, now_iso, error_category, inbox_id, token),
        )


def mark_failed(
    conn: sqlite3.Connection,
    *,
    inbox_id: int,
    token: str,
    now_iso: str,
    error_category: str,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE telegram_inbox SET status = ?, processed_at_utc = ?, "
            "  claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ?, "
            "  last_error_category = ? "
            "WHERE id = ? AND claim_token = ?",
            (STATUS_FAILED, now_iso, now_iso, error_category, inbox_id, token),
        )


def get(conn: sqlite3.Connection, inbox_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM telegram_inbox WHERE id = ?", (inbox_id,)).fetchone()


def release_claims(conn: sqlite3.Connection, *, token: str, now_iso: str) -> int:
    """Return unprocessed claims to the queue when a worker stops early.

    Attempts stay incremented (the work really was attempted), but the row is
    immediately available again instead of waiting for its lease to expire.
    """

    with transaction(conn):
        cursor = conn.execute(
            "UPDATE telegram_inbox SET status = ?, available_at_utc = ?, "
            "  claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ? "
            "WHERE claim_token = ? AND status = ?",
            (STATUS_RETRY, now_iso, now_iso, token, STATUS_PROCESSING),
        )
    return cursor.rowcount
