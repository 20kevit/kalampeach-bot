"""Event capture, pending clarifications, and clarification answers.

Idempotency
-----------
``events.source_message_id`` is UNIQUE. A retried update can never create a
second event for the same Telegram message; the service detects the existing
event and reports ``duplicate`` instead.

Clarification delivery
----------------------
Questions are never sent inside the update-processing transaction. The worker's
dedicated delivery step leases ``pending_delivery`` clarifications, so a Telegram
outage can be retried without duplicating events or losing the ask.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import List, Optional

from .. import repository
from ..ai.types import ClarificationAnswerRequest
from ..telegram.client import TelegramError
from ..timeutil import to_utc_iso
from . import domain, messages
from .context import AppContext
from .notify import Notifier
from .outcomes import ServiceOutcome

logger = logging.getLogger("kalampeach.capture")

MAX_QUESTIONS_PER_CLARIFICATION = 2
MAX_DELIVERY_ATTEMPTS = 3


class CaptureService:
    def __init__(self, ctx: AppContext, notifier: Optional[Notifier] = None) -> None:
        self.ctx = ctx
        self.notifier = notifier or Notifier(ctx)

    # -- new messages ------------------------------------------------------
    def handle_new_message(self, *, message, source_message_id: int, extraction) -> ServiceOutcome:
        if not extraction.is_business_event:
            if extraction.classification == "unclear" and message.chat_type == "private":
                self.notifier.send(message.chat_id, messages.UNCLEAR_MESSAGE, kind="notice")
            return ServiceOutcome(action="ignored", note=extraction.classification)

        message_local_date = self.ctx.local_date_of(message.message_timestamp_utc)
        try:
            assessment = domain.assess_new_event(
                extraction,
                message_local_date=message_local_date,
                default_currency=self.ctx.settings.default_currency,
            )
        except domain.DomainError as exc:
            logger.info("domain_rejected reason=%s", exc.reason)
            return ServiceOutcome(action="ignored", note=exc.reason)

        if assessment.is_complete:
            return self._record_confirmed(
                message=message,
                source_message_id=source_message_id,
                assessment=assessment,
            )
        return self._record_pending(
            message=message,
            source_message_id=source_message_id,
            assessment=assessment,
        )

    def _record_confirmed(self, *, message, source_message_id: int, assessment) -> ServiceOutcome:
        draft = assessment.draft
        storage = draft.storage()
        now_iso = self.ctx.now_iso()

        with self.ctx.db.transaction() as connection:
            existing = repository.find_event_by_source_message(connection, source_message_id)
            if existing is not None:
                repository.set_source_status(
                    connection,
                    source_message_id=source_message_id,
                    processing_status="processed",
                    now=now_iso,
                )
                return ServiceOutcome(action="duplicate", event_id=int(existing["id"]))

            event_id = repository.insert_event(
                connection,
                status="confirmed",
                status_reason=None,
                created_by_telegram_user_id=int(message.sender_id),
                source_message_id=source_message_id,
                now=now_iso,
                **storage,
            )
            repository.insert_revision(
                connection,
                event_id=event_id,
                revision_number=1,
                actor_telegram_user_id=int(message.sender_id),
                change_type="created",
                change_reason="event_created",
                source_message_id=source_message_id,
                clarification_id=None,
                previous_snapshot=None,
                new_snapshot=domain.snapshot_json(draft.snapshot(status="confirmed")),
                validation_status="accepted",
                now=now_iso,
            )
            repository.set_source_status(
                connection,
                source_message_id=source_message_id,
                processing_status="processed",
                now=now_iso,
            )

        if message.chat_type == "private":
            self.notifier.send(
                message.chat_id,
                messages.confirmation_text(draft.snapshot(status="confirmed")),
                kind="event_confirmation",
                event_id=event_id,
            )
        logger.info(
            "event_confirmed event_id=%s type=%s chat_type=%s",
            event_id,
            draft.event_type,
            message.chat_type,
        )
        return ServiceOutcome(action="confirmed", event_id=event_id)

    def _record_pending(self, *, message, source_message_id: int, assessment) -> ServiceOutcome:
        draft = assessment.draft
        storage = draft.storage()
        missing = list(assessment.missing_fields)
        question = messages.clarification_question(missing)
        now_iso = self.ctx.now_iso()

        with self.ctx.db.transaction() as connection:
            existing = repository.find_event_by_source_message(connection, source_message_id)
            if existing is not None:
                repository.set_source_status(
                    connection,
                    source_message_id=source_message_id,
                    processing_status="pending",
                    now=now_iso,
                )
                return ServiceOutcome(action="duplicate", event_id=int(existing["id"]))

            event_id = repository.insert_event(
                connection,
                status="pending_clarification",
                status_reason="missing_fields:" + ",".join(missing),
                created_by_telegram_user_id=int(message.sender_id),
                source_message_id=source_message_id,
                now=now_iso,
                **storage,
            )
            repository.insert_revision(
                connection,
                event_id=event_id,
                revision_number=1,
                actor_telegram_user_id=int(message.sender_id),
                change_type="created",
                change_reason="pending_clarification",
                source_message_id=source_message_id,
                clarification_id=None,
                previous_snapshot=None,
                new_snapshot=domain.snapshot_json(draft.snapshot(status="pending_clarification")),
                validation_status="pending",
                now=now_iso,
            )
            clarification_id = repository.insert_clarification(
                connection,
                event_id=event_id,
                employee_telegram_user_id=int(message.sender_id),
                question_text=question,
                missing_fields=missing,
                now=now_iso,
            )
            repository.set_source_status(
                connection,
                source_message_id=source_message_id,
                processing_status="pending",
                now=now_iso,
            )

        logger.info(
            "event_pending event_id=%s type=%s missing=%s",
            event_id,
            draft.event_type,
            ",".join(missing),
        )
        return ServiceOutcome(
            action="pending", event_id=event_id, clarification_id=clarification_id
        )

    # -- clarification delivery -------------------------------------------
    def deliver_pending_clarifications(self, now=None, *, limit: int = 5) -> dict:
        moment = now or self.ctx.now()
        now_iso = to_utc_iso(moment)
        stats = {"sent": 0, "retry": 0, "unable": 0}

        with self.ctx.db.connection() as connection:
            pending = repository.list_clarifications_for_delivery(
                connection, now=now_iso, limit=limit
            )

        for row in pending:
            outcome = self._deliver_one(row, moment)
            stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def _deliver_one(self, row, moment) -> str:
        clarification_id = int(row["id"])
        employee_id = int(row["employee_telegram_user_id"])
        try:
            message_id = self.ctx.telegram.send_message(employee_id, row["question_text"])
        except TelegramError as exc:
            attempts = int(row["delivery_attempts"]) + 1
            if exc.retryable and attempts < MAX_DELIVERY_ATTEMPTS:
                next_at = to_utc_iso(moment + timedelta(seconds=min(300 * attempts, 1800)))
                self._mark_delivery_failure(clarification_id, exc.category, next_at)
                logger.warning(
                    "clarification_delivery_retry clarification_id=%s attempt=%d category=%s",
                    clarification_id,
                    attempts,
                    exc.category,
                )
                return "retry"
            self._mark_delivery_failure(clarification_id, exc.category, None)
            self._send_setup_hint(int(row["event_id"]))
            logger.warning(
                "clarification_undeliverable clarification_id=%s category=%s",
                clarification_id,
                exc.category,
            )
            return "unable"

        now_iso = to_utc_iso(moment)
        with self.ctx.db.transaction() as connection:
            repository.mark_clarification_delivered(
                connection,
                clarification_id=clarification_id,
                now=now_iso,
                question_count=int(row["question_count"]) + 1,
            )
            repository.record_bot_message(
                connection,
                chat_id=employee_id,
                message_id=int(message_id),
                kind="clarification_prompt",
                event_id=int(row["event_id"]),
                clarification_id=clarification_id,
                now=now_iso,
            )
        logger.info(
            "clarification_sent clarification_id=%s event_id=%s",
            clarification_id,
            row["event_id"],
        )
        return "sent"

    def _mark_delivery_failure(
        self, clarification_id: int, category: str, next_attempt_at: Optional[str]
    ) -> None:
        with self.ctx.db.transaction() as connection:
            repository.mark_clarification_delivery_failed(
                connection,
                clarification_id=clarification_id,
                error_category=category,
                next_attempt_at=next_attempt_at,
                now=self.ctx.now_iso(),
            )

    def _send_setup_hint(self, event_id: int) -> None:
        """Neutral hint in the group — never business details."""

        with self.ctx.db.connection() as connection:
            event = repository.get_event(connection, event_id)
            if event is None:
                return
            source = repository.get_source_message(connection, int(event["source_message_id"]))
        if source is None or source["chat_type"] != "group":
            return
        self.notifier.send(
            int(source["chat_id"]),
            messages.GROUP_SETUP_HINT,
            kind="notice",
            event_id=event_id,
        )

    # -- clarification answers --------------------------------------------
    def handle_answer(self, *, message, source_message_id: int, clarification) -> ServiceOutcome:
        if int(clarification["employee_telegram_user_id"]) != int(message.sender_id or 0):
            return ServiceOutcome(action="ignored", note="answer_not_owner")
        if clarification["status"] != "awaiting_answer":
            return ServiceOutcome(action="ignored", note="clarification_not_awaiting")
        if clarification["response_source_message_id"] == source_message_id:
            return ServiceOutcome(action="duplicate", clarification_id=int(clarification["id"]))

        with self.ctx.db.connection() as connection:
            event = repository.get_event(connection, int(clarification["event_id"]))
            source = repository.get_source_message(connection, int(event["source_message_id"]))
        if event is None:
            return ServiceOutcome(action="ignored", note="event_missing")

        current = domain.draft_from_row(event)
        missing = tuple(json.loads(clarification["missing_fields"]) or ())
        message_local_date = self.ctx.local_date_of(message.message_timestamp_utc)

        answer = self.ctx.ai.complete_clarification(
            ClarificationAnswerRequest(
                original_text=source["message_text"] if source is not None else "",
                question=clarification["question_text"],
                missing_fields=missing,
                answer_text=message.text or "",
                event_type=event["event_type"],
                message_date_local=message_local_date,
            )
        )

        changes = {
            field: value
            for field, value in answer.values.items()
            if field in set(missing) and value is not None
        }
        try:
            assessment = domain.assess_correction(
                current, changes, message_local_date=message_local_date
            )
        except domain.DomainError as exc:
            logger.info("clarification_answer_rejected reason=%s", exc.reason)
            # Never post business details into the group: DM the author instead.
            self.notifier.send(
                int(clarification["employee_telegram_user_id"]),
                messages.CORRECTION_UNCLEAR,
                kind="notice",
            )
            self._expire_or_requeue(
                clarification=clarification,
                event=event,
                source_message_id=source_message_id,
                missing=missing,
                message=message,
                moment=self.ctx.now(),
            )
            return ServiceOutcome(action="pending", clarification_id=int(clarification["id"]))

        if assessment.is_complete:
            return self._apply_complete_answer(
                message=message,
                source_message_id=source_message_id,
                clarification=clarification,
                event=event,
                assessment=assessment,
            )
        return self._expire_or_requeue(
            clarification=clarification,
            event=event,
            source_message_id=source_message_id,
            missing=assessment.missing_fields,
            message=message,
            moment=self.ctx.now(),
        )

    def _apply_complete_answer(
        self, *, message, source_message_id: int, clarification, event, assessment
    ) -> ServiceOutcome:
        now_iso = self.ctx.now_iso()
        new_draft = assessment.draft
        previous = domain.draft_from_row(event)
        with self.ctx.db.transaction() as connection:
            revision_number = repository.next_revision_number(connection, int(event["id"]))
            storage = new_draft.storage()
            repository.update_event_fields(
                connection,
                event_id=int(event["id"]),
                status="confirmed",
                status_reason=None,
                revision_number=revision_number,
                now=now_iso,
                **storage,
            )
            repository.insert_revision(
                connection,
                event_id=int(event["id"]),
                revision_number=revision_number,
                actor_telegram_user_id=int(message.sender_id),
                change_type="clarification",
                change_reason="clarification_answer",
                source_message_id=source_message_id,
                clarification_id=int(clarification["id"]),
                previous_snapshot=domain.snapshot_json(
                    previous.snapshot(status=str(event["status"]))
                ),
                new_snapshot=domain.snapshot_json(new_draft.snapshot(status="confirmed")),
                validation_status="accepted",
                now=now_iso,
            )
            repository.mark_clarification_answered(
                connection,
                clarification_id=int(clarification["id"]),
                response_source_message_id=source_message_id,
                now=now_iso,
            )
            repository.mark_clarification_resolved(
                connection, clarification_id=int(clarification["id"]), now=now_iso
            )
            self._resolve_other_clarifications(connection, int(event["id"]), now_iso)
            repository.set_source_status(
                connection,
                source_message_id=source_message_id,
                processing_status="processed",
                now=now_iso,
            )

        self.notifier.send(
            int(clarification["employee_telegram_user_id"]),
            messages.confirmation_text(new_draft.snapshot(status="confirmed")),
            kind="event_confirmation",
            event_id=int(event["id"]),
        )
        logger.info("clarification_resolved clarification_id=%s", clarification["id"])
        return ServiceOutcome(
            action="confirmed",
            event_id=int(event["id"]),
            clarification_id=int(clarification["id"]),
        )

    def _expire_or_requeue(
        self, *, clarification, event, source_message_id: int, missing, message, moment
    ) -> ServiceOutcome:
        """Ask once more for the remaining fields, then stop and stay pending."""

        now_iso = to_utc_iso(moment)
        clarification_id = int(clarification["id"])
        question_count = int(clarification["question_count"])
        can_ask_again = question_count < MAX_QUESTIONS_PER_CLARIFICATION

        with self.ctx.db.transaction() as connection:
            repository.mark_clarification_answered(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=source_message_id,
                now=now_iso,
            )
            repository.update_event_fields(
                connection,
                event_id=int(event["id"]),
                event_type=str(event["event_type"]),
                status=str(event["status"]),
                event_date=str(event["event_date"]),
                amount=event["amount"],
                currency=event["currency"],
                currency_source=event["currency_source"],
                item_name=event["item_name"],
                quantity=event["quantity"],
                unit=event["unit"],
                inventory_direction=event["inventory_direction"],
                description=event["description"],
                revision_number=int(event["current_revision_number"]),
                status_reason="missing_fields:" + ",".join(missing),
                now=now_iso,
            )
            if can_ask_again:
                repository.requeue_clarification_question(
                    connection,
                    clarification_id=clarification_id,
                    question_text=messages.clarification_question(
                        list(missing),
                        prefix="برای تکمیل این مورد هنوز به این اطلاعات نیاز است:",
                    ),
                    missing_fields=list(missing),
                    now=now_iso,
                )
            else:
                repository.expire_clarification(
                    connection, clarification_id=clarification_id, now=now_iso
                )
            repository.set_source_status(
                connection,
                source_message_id=source_message_id,
                processing_status="pending",
                now=now_iso,
            )

        if not can_ask_again:
            self.notifier.send(
                int(clarification["employee_telegram_user_id"]),
                messages.CLARIFICATION_EXPIRED,
                kind="notice",
            )
            logger.info("clarification_expired clarification_id=%s", clarification_id)
        return ServiceOutcome(
            action="pending",
            event_id=int(event["id"]),
            clarification_id=clarification_id,
        )

    def _resolve_other_clarifications(self, connection, event_id: int, now_iso: str) -> None:
        rows = connection.execute(
            """
            SELECT id FROM clarifications
             WHERE event_id = ? AND status IN ('pending_delivery', 'awaiting_answer')
            """,
            (event_id,),
        ).fetchall()
        for row in rows:
            repository.mark_clarification_resolved(
                connection, clarification_id=int(row["id"]), now=now_iso
            )
