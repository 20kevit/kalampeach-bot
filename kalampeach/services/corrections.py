"""Reply-based natural-language corrections with append-only revisions.

Permission rules (PRODUCT-SPEC section 7):

* the author of the event may correct it;
* the configured manager may correct any event;
* anyone else is refused privately, and no revision is written.

Every accepted correction appends a revision and updates the current projection
in the same transaction. Previous snapshots are never overwritten or deleted.
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import repository
from ..ai.types import CorrectionRequest
from ..timeutil import to_utc_iso
from . import domain, messages
from .context import AppContext
from .notify import Notifier
from .outcomes import ServiceOutcome

logger = logging.getLogger("kalampeach.corrections")


class CorrectionService:
    def __init__(self, ctx: AppContext, notifier: Optional[Notifier] = None) -> None:
        self.ctx = ctx
        self.notifier = notifier or Notifier(ctx)

    def handle(self, *, message, source_message_id: int, event) -> ServiceOutcome:
        event_id = int(event["id"])
        actor = int(message.sender_id or 0)

        if not (
            actor == int(event["created_by_telegram_user_id"])
            or self.ctx.settings.is_manager(actor)
        ):
            self._mark_source(source_message_id, "ignored", "correction_not_permitted")
            self.notifier.send(int(actor), messages.CORRECTION_NOT_ALLOWED, kind="notice")
            logger.info("correction_refused event_id=%s reason=permission", event_id)
            return ServiceOutcome(action="ignored", event_id=event_id, note="not_permitted")

        with self.ctx.db.connection() as connection:
            already = repository.find_events_by_revision_source(connection, source_message_id)
        if already:
            self._mark_source(source_message_id, "processed", None)
            return ServiceOutcome(action="duplicate", event_id=event_id)

        current = domain.draft_from_row(event)
        message_local_date = self.ctx.local_date_of(message.message_timestamp_utc)

        result = self.ctx.ai.interpret_correction(
            CorrectionRequest(
                current_snapshot=current.storage(),
                correction_text=message.text or "",
                message_date_local=message_local_date,
                event_type=str(event["event_type"]),
            )
        )

        if result.interpretation == "not_a_correction":
            self._mark_source(source_message_id, "ignored", "not_a_correction")
            logger.info("correction_ignored event_id=%s reason=not_a_correction", event_id)
            return ServiceOutcome(action="ignored", event_id=event_id, note="not_a_correction")

        if result.interpretation == "unclear" or not result.changes:
            self._mark_source(source_message_id, "ignored", "correction_unclear")
            self.notifier.send(int(actor), messages.CORRECTION_UNCLEAR, kind="notice")
            return ServiceOutcome(action="ignored", event_id=event_id, note="unclear")

        try:
            assessment = domain.assess_correction(
                current, result.changes, message_local_date=message_local_date
            )
        except domain.DomainError as exc:
            self._mark_source(source_message_id, "ignored", "correction_invalid")
            self.notifier.send(int(actor), messages.CORRECTION_UNCLEAR, kind="notice")
            logger.info("correction_invalid event_id=%s reason=%s", event_id, exc.reason)
            return ServiceOutcome(action="ignored", event_id=event_id, note=exc.reason)

        if assessment.missing_fields:
            return self._request_correction_details(
                message=message,
                source_message_id=source_message_id,
                event=event,
                assessment=assessment,
            )
        return self._apply(
            message=message,
            source_message_id=source_message_id,
            event=event,
            assessment=assessment,
            reason=result.reason,
        )

    # -- internals ---------------------------------------------------------
    def _request_correction_details(self, *, message, source_message_id, event, assessment):
        now_iso = self.ctx.now_iso()
        missing = list(assessment.missing_fields)
        event_id = int(event["id"])
        with self.ctx.db.transaction() as connection:
            repository.insert_clarification(
                connection,
                event_id=event_id,
                employee_telegram_user_id=int(message.sender_id),
                question_text=messages.clarification_question(
                    missing,
                    prefix="برای اعمال این اصلاح به اطلاعات بیشتری نیاز است:",
                ),
                missing_fields=missing,
                now=now_iso,
            )
            repository.set_source_status(
                connection,
                source_message_id=source_message_id,
                processing_status="pending",
                now=now_iso,
            )
        logger.info("correction_needs_details event_id=%s missing=%s", event_id, ",".join(missing))
        return ServiceOutcome(action="pending", event_id=event_id)

    def _apply(self, *, message, source_message_id, event, assessment, reason):
        now_iso = self.ctx.now_iso()
        event_id = int(event["id"])
        actor = int(message.sender_id)
        previous = domain.draft_from_row(event)
        new_draft = assessment.draft
        new_status = "confirmed"

        with self.ctx.db.transaction() as connection:
            revision_number = repository.next_revision_number(connection, event_id)
            storage = new_draft.storage()
            repository.update_event_fields(
                connection,
                event_id=event_id,
                status=new_status,
                status_reason=None,
                revision_number=revision_number,
                now=now_iso,
                **storage,
            )
            repository.insert_revision(
                connection,
                event_id=event_id,
                revision_number=revision_number,
                actor_telegram_user_id=actor,
                change_type="correction",
                change_reason=(reason or "correction")[:200],
                source_message_id=source_message_id,
                clarification_id=None,
                previous_snapshot=domain.snapshot_json(
                    previous.snapshot(status=str(event["status"]))
                ),
                new_snapshot=domain.snapshot_json(new_draft.snapshot(status=new_status)),
                validation_status="accepted",
                now=now_iso,
            )
            self._resolve_open_clarifications(connection, event_id, now_iso)
            repository.set_source_status(
                connection,
                source_message_id=source_message_id,
                processing_status="processed",
                now=now_iso,
            )

        self.notifier.send(int(actor), messages.CORRECTION_APPLIED, kind="notice", event_id=event_id)
        logger.info("correction_applied event_id=%s revision=%d", event_id, revision_number)
        return ServiceOutcome(action="corrected", event_id=event_id)

    def _resolve_open_clarifications(self, connection, event_id: int, now_iso: str) -> None:
        rows = connection.execute(
            """
            SELECT id FROM clarifications
             WHERE event_id = ? AND status IN ('pending_delivery', 'awaiting_answer')
            """,
            (event_id,),
        ).fetchall()
        for row in rows:
            # The transition is filtered to non-terminal states, so 0 here means
            # another writer (an answer being applied right now) already closed
            # the row. Never force it; the event correction itself already applied.
            if not repository.mark_clarification_resolved(
                connection, clarification_id=int(row["id"]), now=now_iso
            ):
                logger.info(
                    "clarification_resolution_skipped clarification_id=%s", int(row["id"])
                )

    def _mark_source(self, source_message_id: int, status: str, reason: Optional[str]) -> None:
        with self.ctx.db.transaction() as connection:
            repository.set_source_status(
                connection,
                source_message_id=source_message_id,
                processing_status=status,
                failure_reason=reason,
                now=to_utc_iso(self.ctx.now()),
            )
