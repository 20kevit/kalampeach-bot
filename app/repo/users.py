"""Observed Telegram users (audit only, never authorization)."""

from __future__ import annotations

import sqlite3

from app.db import transaction


def record_observation(
    conn: sqlite3.Connection,
    *,
    telegram_user_id: int,
    display_name: str | None,
    role: str,
    now_iso: str,
) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO users "
            "(telegram_user_id, display_name, role, is_active, created_at_utc, updated_at_utc) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "  display_name = COALESCE(excluded.display_name, users.display_name), "
            "  role = excluded.role, "
            "  updated_at_utc = excluded.updated_at_utc",
            (telegram_user_id, display_name, role, now_iso, now_iso),
        )
