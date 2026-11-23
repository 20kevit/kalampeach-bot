"""Read access to the append-only revision history."""

from __future__ import annotations

import sqlite3


def list_for_event(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY revision_number",
            (event_id,),
        ).fetchall()
    )


def get(conn: sqlite3.Connection, revision_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM event_revisions WHERE id = ?", (revision_id,)
    ).fetchone()
