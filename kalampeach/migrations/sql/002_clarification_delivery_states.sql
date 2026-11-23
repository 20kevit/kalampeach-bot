-- 002: clarification delivery claims and an explicit "unknown outcome" state.
--
-- Why this migration exists
--   The first schema could not represent two states that Telegram makes
--   unavoidable, so the delivery step had to guess:
--
--     * "sending"          the worker has claimed this question and is about to
--                          call Telegram. A process killed here leaves an
--                          in-flight send whose result nobody recorded.
--     * "delivery_uncertain" the send was attempted but the result is unknown
--                          (timeout, connection reset after write, or a crash
--                          between send and bookkeeping). It must never be
--                          resent automatically.
--
--   SQLite cannot alter a CHECK constraint in place, so the table is rebuilt
--   while preserving every row, id, timestamp and foreign key. Nothing else in
--   the schema references clarifications, so the rebuild is self-contained and
--   runs inside the single transaction the migration runner opens.
--
-- This migration is forward-only and additive in meaning: it never drops data.

CREATE TABLE clarifications_new (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                    INTEGER NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    employee_telegram_user_id   INTEGER NOT NULL,
    question_text               TEXT NOT NULL,
    missing_fields              TEXT NOT NULL,
    status                      TEXT NOT NULL
                                CHECK (status IN ('pending_delivery', 'sending', 'awaiting_answer',
                                                  'answered', 'resolved', 'expired',
                                                  'unable_to_deliver', 'delivery_uncertain')),
    question_count              INTEGER NOT NULL DEFAULT 0,
    delivery_attempts           INTEGER NOT NULL DEFAULT 0,
    next_delivery_attempt_at_utc TEXT,
    last_delivery_error         TEXT,
    lease_owner                 TEXT,
    lease_expires_at_utc        TEXT,
    response_source_message_id  INTEGER REFERENCES source_messages (id) ON DELETE SET NULL,
    created_at_utc              TEXT NOT NULL,
    updated_at_utc              TEXT NOT NULL,
    answered_at_utc             TEXT,
    resolved_at_utc             TEXT
);

INSERT INTO clarifications_new (
    id, event_id, employee_telegram_user_id, question_text, missing_fields, status,
    question_count, delivery_attempts, next_delivery_attempt_at_utc, last_delivery_error,
    response_source_message_id, created_at_utc,
    updated_at_utc, answered_at_utc, resolved_at_utc
)
SELECT
    id, event_id, employee_telegram_user_id, question_text, missing_fields, status,
    question_count, delivery_attempts, next_delivery_attempt_at_utc, last_delivery_error,
    response_source_message_id, created_at_utc,
    updated_at_utc, answered_at_utc, resolved_at_utc
FROM clarifications;

DROP TABLE clarifications;

ALTER TABLE clarifications_new RENAME TO clarifications;

CREATE INDEX ix_clarifications_delivery
    ON clarifications (status, next_delivery_attempt_at_utc);
CREATE INDEX ix_clarifications_employee
    ON clarifications (employee_telegram_user_id, status);
CREATE INDEX ix_clarifications_lease
    ON clarifications (status, lease_expires_at_utc);
