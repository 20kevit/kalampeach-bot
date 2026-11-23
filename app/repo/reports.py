"""Report delivery state (daily and on-demand).

``report_key`` is the idempotency key. A row is claimed with a lease before the
message is sent, so overlapping cron invocations cannot send the same report
twice. ``uncertain`` is a deliberate terminal-ish state for a timed-out send
where Telegram may already have accepted the message: retrying blindly could
duplicate it, so an operator decides.
"""

from __future__ import annotations

import sqlite3

from app.db import transaction

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_UNCERTAIN = "uncertain"


def ensure(
    conn: sqlite3.Connection,
    *,
    report_key: str,
    report_type: str,
    manager_telegram_user_id: int,
    period_start_date: str,
    period_end_date: str,
    now_iso: str,
) -> tuple[sqlite3.Row, bool]:
    """Create the delivery row once. Returns ``(row, created)``."""

    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO report_deliveries "
            "(report_key, report_type, manager_telegram_user_id, period_start_date, "
            " period_end_date, status, attempts, next_attempt_at_utc, "
            " created_at_utc, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?) "
            "ON CONFLICT(report_key) DO NOTHING",
            (
                report_key,
                report_type,
                manager_telegram_user_id,
                period_start_date,
                period_end_date,
                STATUS_PENDING,
                now_iso,
                now_iso,
                now_iso,
            ),
        )
        created = cursor.rowcount == 1
        row = conn.execute(
            "SELECT * FROM report_deliveries WHERE report_key = ?", (report_key,)
        ).fetchone()
    return row, created


def claim(
    conn: sqlite3.Connection,
    *,
    report_key: str,
    token: str,
    now_iso: str,
    lease_until_iso: str,
    max_attempts: int,
) -> sqlite3.Row | None:
    """Claim one delivery row for sending, or return ``None``."""

    with transaction(conn):
        cursor = conn.execute(
            "UPDATE report_deliveries "
            "SET status = ?, claim_token = ?, claimed_until_utc = ?, "
            "    attempts = attempts + 1, updated_at_utc = ? "
            "WHERE report_key = ? AND attempts < ? AND ("
            "  (status IN (?, ?) AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= ?)) "
            "  OR (status = ? AND (claimed_until_utc IS NULL OR claimed_until_utc <= ?))"
            ")",
            (
                STATUS_SENDING,
                token,
                lease_until_iso,
                now_iso,
                report_key,
                max_attempts,
                STATUS_PENDING,
                STATUS_FAILED,
                now_iso,
                STATUS_SENDING,
                now_iso,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM report_deliveries WHERE report_key = ? AND claim_token = ?",
            (report_key, token),
        ).fetchone()


def mark_sent(
    conn: sqlite3.Connection,
    *,
    report_key: str,
    token: str,
    now_iso: str,
    telegram_message_id: int | None,
    fact_snapshot_json: str,
    narrative_source: str,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE report_deliveries SET status = ?, telegram_message_id = ?, "
            "  fact_snapshot_json = ?, narrative_source = ?, sent_at_utc = ?, "
            "  claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ?, "
            "  last_error_category = NULL "
            "WHERE report_key = ? AND claim_token = ?",
            (
                STATUS_SENT,
                telegram_message_id,
                fact_snapshot_json,
                narrative_source,
                now_iso,
                now_iso,
                report_key,
                token,
            ),
        )


def mark_failed(
    conn: sqlite3.Connection,
    *,
    report_key: str,
    token: str,
    now_iso: str,
    category: str,
    next_attempt_iso: str | None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE report_deliveries SET status = ?, last_error_category = ?, "
            "  next_attempt_at_utc = ?, claim_token = NULL, claimed_until_utc = NULL, "
            "  updated_at_utc = ? WHERE report_key = ? AND claim_token = ?",
            (STATUS_FAILED, category, next_attempt_iso, now_iso, report_key, token),
        )


def mark_uncertain(
    conn: sqlite3.Connection,
    *,
    report_key: str,
    token: str,
    now_iso: str,
    category: str,
    fact_snapshot_json: str | None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE report_deliveries SET status = ?, last_error_category = ?, "
            "  claim_token = NULL, claimed_until_utc = NULL, updated_at_utc = ?, "
            "  fact_snapshot_json = COALESCE(?, fact_snapshot_json) "
            "WHERE report_key = ? AND claim_token = ?",
            (STATUS_UNCERTAIN, category, now_iso, fact_snapshot_json, report_key, token),
        )


def get(conn: sqlite3.Connection, report_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM report_deliveries WHERE report_key = ?", (report_key,)
    ).fetchone()


def list_recent(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM report_deliveries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    )
