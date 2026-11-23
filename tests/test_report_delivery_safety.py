"""Regression tests for multi-part report delivery safety (TEST-PLAN F).

These cover defects reproduced against the previous implementation: a chunk with
an unknown outcome was resent, and a lost snapshot caused confirmed progress to
be discarded. They also pin the properties that already held (frozen content,
per-chunk progress, idempotency under overlap).
"""

from __future__ import annotations

import json
threading = __import__("threading")

import pytest

from kalampeach.services import messages, reporting
from kalampeach.services.reports import ReportService
from kalampeach.telegram.client import TelegramError, chunk_text
from conftest import EMPLOYEE_ID, GROUP_ID, MANAGER_ID
from helpers import (
    fetch_one,
    manager_update,
    report_deliveries,
    run_worker,
    seed_event,
    seed_report_delivery,
    with_worker,
)


def at(ctx, iso: str) -> None:
    from datetime import datetime

    ctx.clock.set(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def row_of(ctx, delivery_id: int):
    return fetch_one(ctx, "SELECT * FROM report_deliveries WHERE id = ?", (delivery_id,))


def report_sends(fake_telegram):
    """Chunk sends to the manager, excluding the "outcome unknown" notice."""

    return [
        item
        for item in fake_telegram.attempts
        if item["chat_id"] == MANAGER_ID and item["text"] != messages.REPORT_DELIVERY_UNKNOWN
    ]


def test_unknown_in_flight_chunk_is_never_resent(ctx, fake_telegram):
    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(
        ctx,
        status="sending",
        chunks=2,
        chunks_sent=1,
        lease_expires_at="2026-11-23T20:04:00Z",
    )

    summary = ReportService(ctx).run_due()

    assert summary["deliveries"] == {"uncertain": 1}
    row = row_of(ctx, delivery_id)
    assert row["status"] == "failed_uncertain"
    assert row["last_error_category"] == "chunk_outcome_unknown"
    assert report_sends(fake_telegram) == [], "the unknown chunk must not be resent"
    assert messages.REPORT_DELIVERY_UNKNOWN in fake_telegram.texts_to(MANAGER_ID)

    # A later run must not resurrect it either.
    at(ctx, "2026-11-23T21:00:00Z")
    assert ReportService(ctx).run_due()["deliveries"] == {}
    assert report_sends(fake_telegram) == []


def test_interrupted_after_all_chunks_is_finalised_without_resending(ctx, fake_telegram):
    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(
        ctx,
        status="sending",
        chunks=2,
        chunks_sent=2,
        lease_expires_at="2026-11-23T20:04:00Z",
    )

    summary = ReportService(ctx).run_due()

    assert summary["deliveries"] == {"sent_recovered": 1}
    assert row_of(ctx, delivery_id)["status"] == "sent"
    assert report_sends(fake_telegram) == []


def test_interrupted_before_any_send_is_retried_safely(ctx, fake_telegram):
    at(ctx, "2026-11-23T20:05:00Z")
    seed_event(ctx, 1, amount="1000")
    delivery_id = seed_report_delivery(
        ctx,
        status="sending",
        chunks=0,
        chunks_sent=0,
        attempts=0,
        lease_expires_at="2026-11-23T20:04:00Z",
    )

    summary = ReportService(ctx).run_due()

    assert summary["deliveries"] == {"sent": 1}
    row = row_of(ctx, delivery_id)
    assert row["status"] == "sent"
    assert len(report_sends(fake_telegram)) == row["chunk_count"]


def test_live_lease_is_not_stolen(ctx, fake_telegram):
    at(ctx, "2026-11-23T20:05:00Z")
    seed_event(ctx, 1, amount="1000")
    delivery_id = seed_report_delivery(
        ctx,
        status="sending",
        chunks=2,
        chunks_sent=0,
        lease_expires_at="2026-11-23T20:10:00Z",
    )

    summary = ReportService(ctx).run_due()

    assert summary["deliveries"] == {"busy": 1}
    assert row_of(ctx, delivery_id)["status"] == "sending"
    assert report_sends(fake_telegram) == []


def test_missing_snapshot_with_confirmed_chunks_is_not_rebuilt(ctx, fake_telegram):
    at(ctx, "2026-11-23T20:05:00Z")
    seed_event(ctx, 1, amount="1000")
    delivery_id = seed_report_delivery(
        ctx, status="pending", chunks=2, chunks_sent=1, snapshot=False
    )

    summary = ReportService(ctx).run_due()

    assert summary["deliveries"] == {"uncertain": 1}
    row = row_of(ctx, delivery_id)
    assert row["status"] == "failed_uncertain"
    assert row["last_error_category"] == "snapshot_unavailable_with_confirmed_chunks"
    assert report_sends(fake_telegram) == [], "confirmed content must not be resent"
    assert messages.REPORT_DELIVERY_UNKNOWN in fake_telegram.texts_to(MANAGER_ID)


def test_corrupt_snapshot_with_confirmed_chunks_is_not_rebuilt(ctx, fake_telegram):
    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(
        ctx, status="pending", chunks=2, chunks_sent=1, snapshot=False
    )
    with ctx.db.transaction() as connection:
        connection.execute(
            "UPDATE report_deliveries SET snapshot_json = '{not json' WHERE id = ?",
            (delivery_id,),
        )

    assert ReportService(ctx).run_due()["deliveries"] == {"uncertain": 1}
    assert report_sends(fake_telegram) == []


def test_inconsistent_progress_is_not_resent(ctx, fake_telegram):
    at(ctx, "2026-11-23T20:05:00Z")
    delivery_id = seed_report_delivery(ctx, status="pending", chunks=2, chunks_sent=5)

    summary = ReportService(ctx).run_due()

    assert summary["deliveries"] == {"uncertain": 1}
    assert row_of(ctx, delivery_id)["status"] == "failed_uncertain"
    assert report_sends(fake_telegram) == []


def test_report_content_is_frozen_before_the_first_send(ctx, fake_telegram, monkeypatch):
    for index in range(12):
        seed_event(
            ctx,
            index + 1,
            event_type="operational_note",
            amount=None,
            currency=None,
            description="یادداشت %d: %s" % (index + 1, "شرح طولانی " * 30),
        )
    at(ctx, "2026-11-23T20:00:05Z")

    real_send = fake_telegram.send_message
    calls = {"n": 0}

    def flaky(chat_id, text, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # first chunk confirmed, second attempt fails retryably
            raise TelegramError("telegram_unavailable", retryable=True)
        return real_send(chat_id, text, **kwargs)

    monkeypatch.setattr(fake_telegram, "send_message", flaky)
    first = ReportService(ctx).run_due()

    assert first["deliveries"] == {"retry": 1}
    row = row_of(ctx, 1)
    assert row["status"] == "pending"
    assert row["chunks_sent"] == 1
    frozen = json.loads(row["snapshot_json"])["text"]
    assert row["chunk_count"] == len(chunk_text(frozen)) > 1

    # Business data changes after the report was computed.
    seed_event(
        ctx,
        99,
        event_type="sale_revenue",
        amount="987654",
        currency="تومان",
        description="فروش بعد از برش گزارش",
    )

    monkeypatch.setattr(fake_telegram, "send_message", real_send)
    at(ctx, "2026-11-23T20:06:00Z")
    second = ReportService(ctx).run_due()

    assert second["deliveries"] == {"sent": 1}
    row = row_of(ctx, 1)
    assert row["status"] == "sent"
    assert row["chunks_sent"] == row["chunk_count"]
    assert json.loads(row["snapshot_json"])["text"] == frozen, "content must not be recomputed"

    delivered = "\n".join(item["text"] for item in report_sends(fake_telegram))
    assert "987654" not in delivered
    # Every chunk was recorded exactly once: the confirmed first chunk was not
    # repeated when the delivery resumed, and the failing attempt never landed.
    assert len(report_sends(fake_telegram)) == row["chunk_count"]


def test_retry_resumes_after_the_last_confirmed_chunk(ctx, fake_telegram, monkeypatch):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")

    real_send = fake_telegram.send_message
    state = {"count": 0}

    def flaky(chat_id, text, **kwargs):
        state["count"] += 1
        if state["count"] in (2, 3):
            raise TelegramError("telegram_unavailable", retryable=True)
        return real_send(chat_id, text, **kwargs)

    monkeypatch.setattr(fake_telegram, "send_message", flaky)
    ReportService(ctx).run_due()
    monkeypatch.setattr(fake_telegram, "send_message", real_send)

    for minute in (6, shall_not_matter := 12):
        at(ctx, "2026-11-23T20:%02d:00Z" % minute)
        ReportService(ctx).run_due()

    row = row_of(ctx, 1)
    assert row["status"] == "sent"
    assert row["chunks_sent"] == row["chunk_count"]
    assert len(report_sends(fake_telegram)) == row["chunk_count"], "no chunk is delivered twice"


def test_overlapping_workers_deliver_the_daily_report_once(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")
    results = []
    errors = []

    def work(index: int):
        try:
            results.append(ReportService(with_worker(ctx, "worker-%d" % index)).run_due())
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(report_deliveries(ctx)) == 1
    assert report_deliveries(ctx)[0]["status"] == "sent"
    assert len(fake_telegram.texts_to(MANAGER_ID)) == 1


def test_daily_creation_is_counted_once_under_overlap(ctx, fake_telegram):
    seed_event(ctx, 1, amount="1000")
    at(ctx, "2026-11-23T20:00:05Z")
    totals = []

    def work(index: int):
        totals.append(ReportService(with_worker(ctx, "w-%d" % index)).run_due()["daily_created"])

    threads = [threading.Thread(target=work, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sum(totals) == 1, "only the run that really inserted the row counts it"
