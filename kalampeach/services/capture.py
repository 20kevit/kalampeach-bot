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


def clarification_accepts_answer(row, now_iso: str) -> bool:
    """Can an incoming message be treated as the answer to this question?

    ``awaiting_answer`` is the normal case. ``sending`` and ``delivery_uncertain``
    are accepted on purpose: Telegram may already have delivered the question
    while the local bookkeeping has not committed yet (or never will, if the
    sending worker died). The employee can only reply to a question they actually
    received, so rejecting those states would silently discard a valid answer.

    Accepting a live ``sending`` row cannot produce two answers: the sending
    worker's own transition is guarded by ``status = 'sending' AND lease_owner``,
    so the answer wins and the sender simply does not re-apply its delivery state.

    ``now_iso`` is kept for call-site compatibility and for callers that want to
    reason about lease freshness; it no longer gates acceptance.
    """

    return str(row["status"]) in ("awaiting_answer", "sending", "delivery_uncertain")


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
        """Deliver due questions. Independent of the Telegram inbox.

        Each question is claimed (``pending_delivery`` -> ``sending``) *before*
        Telegram is called, so overlapping cron runs cannot send the same
        question twice, and a process killed mid-send leaves a durable trace
        instead of an apparently untouched row.
        """

        moment = now or self.ctx.now()
        now_iso = to_utc_iso(moment)
        stats = {"sent": 0, "retry": 0, "unable": 0, "uncertain": 0, "skipped": 0}

        with self.ctx.db.transaction() as connection:
            stale = repository.expire_stale_clarification_deliveries(connection, now=now_iso)
        for clarification_id in stale:
            logger.error(
                "clarification_delivery_uncertain clarification_id=%s category=lease_expired_during_send",
                clarification_id,
            )

        with self.ctx.db.connection() as connection:
            pending = repository.list_clarifications_for_delivery(
                connection, now=now_iso, limit=limit
            )

        for row in pending:
            outcome = self._deliver_one(int(row["id"]), moment)
            stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def _deliver_one(self, clarification_id: int, moment) -> str:
        now_iso = to_utc_iso(moment)
        lease_seconds = int(self.ctx.settings.worker_lease_seconds)

        with self.ctx.db.transaction() as connection:
            claimed = repository.claim_clarification_for_delivery(
                connection,
                clarification_id=clarification_id,
                now=now_iso,
                lease_seconds=lease_seconds,
                worker_id=self.ctx.worker_id,
            )
            if not claimed:
                # Another run took it, or it stopped being due while we looked.
                return "skipped"
            row = repository.get_clarification(connection, clarification_id)
        if row is None:  # pragma: no cover - defensive
            return "skipped"
        employee_id = int(row["employee_telegram_user_id"])

        try:
            message_id = self.ctx.telegram.send_message(employee_id, row["question_text"])
        except TelegramError as exc:
            return self._handle_send_failure(row, exc, moment, claimed_attempts=int(row["delivery_attempts"]))

        question_count = int(row["question_count"]) + 1
        try:
            with self.ctx.db.transaction() as connection:
                changed = repository.mark_clarification_delivered(
                    connection,
                    clarification_id=clarification_id,
                    now=now_iso,
                    question_count=question_count,
                    worker_id=self.ctx.worker_id,
                )
                # The message definitively exists in Telegram, so its id is always
                # recorded — even if the state transition above lost a race. That
                # keeps later replies resolvable through the normal mapping path.
                repository.record_bot_message(
                    connection,
                    chat_id=employee_id,
                    message_id=int(message_id),
                    kind="clarification_prompt",
                    event_id=int(row["event_id"]),
                    clarification_id=clarification_id,
                    now=now_iso,
                )
        except Exception as exc:  # bookkeeping failed after a confirmed send
            self._mark_uncertain_after_success(clarification_id, exc)
            return "uncertain"

        if not changed:
            # Not an unknown outcome: the row moved on while we were sending, most
            # often because the employee answered the question immediately.
            logger.info(
                "clarification_delivered_after_state_change clarification_id=%s",
                clarification_id,
            )
            return "answered_during_send"

        logger.info(
            "clarification_sent clarification_id=%s event_id=%s",
            clarification_id,
            row["event_id"],
        )
        return "sent"

    def _handle_send_failure(self, row, exc: TelegramError, moment, *, claimed_attempts: int) -> str:
        clarification_id = int(row["id"])
        now_iso = to_utc_iso(moment)
        worker_id = self.ctx.worker_id

        if exc.uncertain:
            # Telegram may have delivered this. Never resend; keep the question
            # answerable so the employee can still complete it by reply.
            with self.ctx.db.transaction() as connection:
                repository.mark_clarification_delivery_uncertain(
                    connection,
                    clarification_id=clarification_id,
                    error_category=exc.category,
                    now=now_iso,
                    worker_id=worker_id,
                )
            logger.error(
                "clarification_delivery_uncertain clarification_id=%s category=%s",
                clarification_id,
                exc.category,
            )
            return "uncertain"

        if exc.retryable and claimed_attempts < MAX_DELIVERY_ATTEMPTS:
            next_at = to_utc_iso(moment + timedelta(seconds=min(300 * claimed_attempts, 1800)))
            with self.ctx.db.transaction() as connection:
                repository.release_clarification_for_retry(
                    connection,
                    clarification_id=clarification_id,
                    error_category=exc.category,
                    next_attempt_at=next_at,
                    now=now_iso,
                    worker_id=worker_id,
                )
            logger.warning(
                "clarification_delivery_retry clarification_id=%s attempt=%d category=%s",
                clarification_id,
                claimed_attempts,
                exc.category,
            )
            return "retry"

        # Definitely not delivered (permanent rejection or attempts exhausted).
        with self.ctx.db.transaction() as connection:
            repository.mark_clarification_undeliverable(
                connection,
                clarification_id=clarification_id,
                error_category=exc.category,
                now=now_iso,
                worker_id=worker_id,
            )
        self._send_setup_hint(int(row["event_id"]))
        logger.warning(
            "clarification_undeliverable clarification_id=%s category=%s attempts=%d",
            clarification_id,
            exc.category,
            claimed_attempts,
        )
        return "unable"

    def _mark_uncertain_after_success(self, clarification_id: int, exc: Exception) -> None:
        """A confirmed send could not be recorded: treat the outcome as unknown."""

        logger.error(
            "clarification_delivery_uncertain clarification_id=%s category=bookkeeping_failed error=%s",
            clarification_id,
            exc.__class__.__name__,
        )
        try:
            with self.ctx.db.transaction() as connection:
                changed = repository.mark_clarification_delivery_uncertain(
                    connection,
                    clarification_id=clarification_id,
                    error_category="bookkeeping_failed",
                    now=self.ctx.now_iso(),
                    worker_id=self.ctx.worker_id,
                )
            if not changed:
                # Rejected: this worker no longer owns the lease, or the row already
                # reached a terminal state (most often the employee answered while
                # the send was in flight). Keep that state; never resend.
                logger.info(
                    "clarification_uncertain_not_recorded clarification_id=%s reason=state_not_eligible",
                    clarification_id,
                )
        except Exception as inner:  # pragma: no cover - database is down
            logger.critical(
                "clarification_state_write_failed clarification_id=%s error=%s",
                clarification_id,
                inner.__class__.__name__,
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
        if not clarification_accepts_answer(clarification, self.ctx.now_iso()):
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
            # Sent only once this answer actually owns the clarification, so a
            # superseded answer cannot produce an extra notice.
            self._expire_or_requeue(
                clarification=clarification,
                event=event,
                source_message_id=source_message_id,
                missing=missing,
                message=message,
                moment=self.ctx.now(),
                unclear_notice=True,
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
        clarification_id = int(clarification["id"])
        applied = False
        with self.ctx.db.transaction() as connection:
            # Claim first and in the same transaction as the writes it authorizes.
            # The claim, the revision and the resolution commit or roll back as a
            # unit, so a failure below cannot leave a clarification claimed-but-not
            # -applied, and two different answers cannot both apply.
            claimed = repository.claim_clarification_answer(
                connection,
                clarification_id=clarification_id,
                response_source_message_id=source_message_id,
                now=now_iso,
            )
            if not claimed:
                logger.info(
                    "clarification_answer_claim_lost clarification_id=%s source_message_id=%s",
                    clarification_id,
                    source_message_id,
                )
            else:
                applied = True
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
                    clarification_id=clarification_id,
                    previous_snapshot=domain.snapshot_json(
                        previous.snapshot(status=str(event["status"]))
                    ),
                    new_snapshot=domain.snapshot_json(new_draft.snapshot(status="confirmed")),
                    validation_status="accepted",
                    now=now_iso,
                )
                resolved = repository.mark_clarification_resolved(
                    connection, clarification_id=clarification_id, now=now_iso
                )
                if not resolved:  # pragma: no cover - the claim above guarantees it
                    raise RuntimeError(
                        "clarification claim lost while resolving %s" % clarification_id
                    )
                self._resolve_other_clarifications(connection, int(event["id"]), now_iso)
                repository.set_source_status(
                    connection,
                    source_message_id=source_message_id,
                    processing_status="processed",
                    now=now_iso,
                )

        if not applied:
            return self._reject_lost_answer(
                clarification_id=clarification_id, source_message_id=source_message_id
            )

        self.notifier.send(
            int(clarification["employee_telegram_user_id"]),
            messages.confirmation_text(new_draft.snapshot(status="confirmed")),
            kind="event_confirmation",
            event_id=int(event["id"]),
        )
        logger.info("clarification_resolved clarification_id=%s", clarification_id)
        return ServiceOutcome(
            action="confirmed",
            event_id=int(event["id"]),
            clarification_id=clarification_id,
        )

    def _reject_lost_answer(self, *, clarification_id: int, source_message_id: int) -> ServiceOutcome:
        """Another answer already owns this clarification: change nothing else.

        Reaching here means the question was answered, resolved or closed between
        the state check at the start of ``handle_answer`` and this write. The losing
        answer must not create a revision, touch the event or send a confirmation.
        """

        try:
            with self.ctx.db.transaction() as connection:
                repository.set_source_status(
                    connection,
                    source_message_id=source_message_id,
                    processing_status="ignored",
                    failure_reason="answer_claim_lost",
                    now=self.ctx.now_iso(),
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("source_status_failed error=%s", exc.__class__.__name__)
        return ServiceOutcome(
            action="duplicate", clarification_id=clarification_id, note="answer_claim_lost"
        )

    def _expire_or_requeue(
        self,
        *,
        clarification,
        event,
        source_message_id: int,
        missing,
        message,
        moment,
        unclear_notice: bool = False,
    ) -> ServiceOutcome:
        """Ask once more for the remaining fields, then stop and stay pending."""

        now_iso = to_utc_iso(moment)
        clarification_id = int(clarification["id"])
        question_count = int(clarification["question_count"])
        can_ask_again = question_count < MAX_QUESTIONS_PER_CLARIFICATION

        claimed = False
        with self.ctx.db.transaction() as connection:
            # Asking again is also a consequence of this specific answer, so it
            # needs the same exclusive claim: two concurrent partial answers must
            # not both requeue or both expire the question. Claim, event note and
            # requeue/expire share one transaction so a failure cannot strand the
            # row in ``answered`` with no question behind it.
            claimed = bool(
                repository.claim_clarification_answer(
                    connection,
                    clarification_id=clarification_id,
                    response_source_message_id=source_message_id,
                    now=now_iso,
                )
            )
            if claimed:
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
                    requeued = repository.requeue_clarification_question(
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
                    requeued = repository.expire_clarification(
                        connection, clarification_id=clarification_id, now=now_iso
                    )
                if not requeued:  # pragma: no cover - the claim above guarantees it
                    raise RuntimeError(
                        "clarification claim lost while reopening %s" % clarification_id
                    )
                repository.set_source_status(
                    connection,
                    source_message_id=source_message_id,
                    processing_status="pending",
                    now=now_iso,
                )

        if not claimed:
            return self._reject_lost_answer(
                clarification_id=clarification_id, source_message_id=source_message_id
            )

        if unclear_notice:
            # Only the answer that actually owns the clarification is told that it
            # was not usable; a superseded answer stays silent.
            self.notifier.send(
                int(clarification["employee_telegram_user_id"]),
                messages.CORRECTION_UNCLEAR,
                kind="notice",
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
             WHERE event_id = ?
               AND status IN ('pending_delivery', 'sending', 'awaiting_answer', 'delivery_uncertain')
            """,
            (event_id,),
        ).fetchall()
        for row in rows:
            repository.mark_clarification_resolved(
                connection, clarification_id=int(row["id"]), now=now_iso
            )
