-- KalamPeach Telegram operations bot: initial schema (migration 001).
--
-- This database is owned exclusively by this project. It must never be linked
-- to, copied from, or shared with the separate KalamPeach accounting web
-- application.
--
-- Conventions:
--   * timestamps: TEXT, canonical UTC ISO-8601 (YYYY-MM-DDTHH:MM:SSZ);
--   * business dates: TEXT, YYYY-MM-DD in Asia/Tehran;
--   * money and quantities: TEXT decimal strings (never REAL/float);
--   * unknown values are NULL, never 0.

CREATE TABLE users (
    telegram_user_id  INTEGER PRIMARY KEY,
    display_name      TEXT,
    role              TEXT NOT NULL CHECK (role IN ('employee', 'manager')),
    is_active         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at_utc    TEXT NOT NULL,
    updated_at_utc    TEXT NOT NULL
);

-- Durable webhook inbox / queue. update_id is the idempotency key.
CREATE TABLE telegram_updates (
    update_id             INTEGER PRIMARY KEY,
    received_at_utc       TEXT NOT NULL,
    raw_payload           TEXT NOT NULL,
    chat_id               INTEGER,
    chat_type             TEXT,
    status                TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'processing', 'done', 'failed')),
    attempts              INTEGER NOT NULL DEFAULT 0,
    available_at_utc      TEXT NOT NULL,
    lease_owner           TEXT,
    lease_expires_at_utc  TEXT,
    last_error_category   TEXT,
    last_error_detail     TEXT,
    completed_at_utc      TEXT,
    updated_at_utc        TEXT NOT NULL
);

CREATE INDEX ix_telegram_updates_queue
    ON telegram_updates (status, available_at_utc);

-- Provenance of every Telegram message the bot considered.
CREATE TABLE source_messages (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_update_id         INTEGER,
    chat_id                    INTEGER NOT NULL,
    chat_type                  TEXT NOT NULL
                               CHECK (chat_type IN ('group', 'private', 'channel', 'other')),
    message_id                 INTEGER NOT NULL,
    reply_to_message_id        INTEGER,
    sender_telegram_user_id    INTEGER,
    message_text               TEXT NOT NULL DEFAULT '',
    message_timestamp_utc      TEXT NOT NULL,
    processing_status          TEXT NOT NULL
                               CHECK (processing_status IN
                                      ('received', 'processing', 'processed', 'ignored', 'pending', 'failed')),
    failure_reason             TEXT,
    created_at_utc             TEXT NOT NULL,
    updated_at_utc             TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);

CREATE UNIQUE INDEX ux_source_messages_update
    ON source_messages (telegram_update_id)
    WHERE telegram_update_id IS NOT NULL;

CREATE TABLE events (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type                  TEXT NOT NULL
                                CHECK (event_type IN ('sale_revenue', 'expense_purchase',
                                                      'inventory_movement', 'operational_note')),
    status                      TEXT NOT NULL
                                CHECK (status IN ('confirmed', 'pending_clarification', 'rejected')),
    event_date                  TEXT NOT NULL,
    amount                      TEXT,
    currency                    TEXT,
    item_name                   TEXT,
    quantity                    TEXT,
    unit                        TEXT,
    inventory_direction         TEXT
                                CHECK (inventory_direction IS NULL OR inventory_direction IN
                                       ('received', 'consumed', 'wasted', 'adjustment')),
    description                 TEXT,
    created_by_telegram_user_id INTEGER NOT NULL,
    source_message_id           INTEGER NOT NULL UNIQUE
                                REFERENCES source_messages (id) ON DELETE RESTRICT,
    current_revision_number     INTEGER NOT NULL DEFAULT 1,
    status_reason               TEXT,
    currency_source             TEXT
                                CHECK (currency_source IS NULL OR currency_source IN
                                       ('explicit', 'default', 'unknown')),
    created_at_utc              TEXT NOT NULL,
    updated_at_utc              TEXT NOT NULL
);

CREATE INDEX ix_events_period ON events (status, event_date);
CREATE INDEX ix_events_author ON events (created_by_telegram_user_id);

-- Append-only history. Rows are never updated or deleted by the application.
CREATE TABLE event_revisions (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                   INTEGER NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    revision_number            INTEGER NOT NULL,
    actor_telegram_user_id     INTEGER NOT NULL,
    change_type                TEXT NOT NULL
                               CHECK (change_type IN ('created', 'correction', 'clarification',
                                                      'status_change')),
    change_reason              TEXT,
    source_message_id          INTEGER REFERENCES source_messages (id) ON DELETE SET NULL,
    clarification_id           INTEGER,
    previous_snapshot          TEXT,
    new_snapshot               TEXT NOT NULL,
    validation_status          TEXT NOT NULL
                               CHECK (validation_status IN ('accepted', 'pending', 'rejected')),
    created_at_utc             TEXT NOT NULL,
    UNIQUE (event_id, revision_number)
);

CREATE TABLE clarifications (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                    INTEGER NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    employee_telegram_user_id   INTEGER NOT NULL,
    question_text               TEXT NOT NULL,
    missing_fields              TEXT NOT NULL,
    status                      TEXT NOT NULL
                                CHECK (status IN ('pending_delivery', 'awaiting_answer', 'answered',
                                                  'resolved', 'expired', 'unable_to_deliver')),
    question_count              INTEGER NOT NULL DEFAULT 0,
    delivery_attempts           INTEGER NOT NULL DEFAULT 0,
    next_delivery_attempt_at_utc TEXT,
    last_delivery_error         TEXT,
    response_source_message_id  INTEGER REFERENCES source_messages (id) ON DELETE SET NULL,
    created_at_utc              TEXT NOT NULL,
    updated_at_utc              TEXT NOT NULL,
    answered_at_utc             TEXT,
    resolved_at_utc             TEXT
);

CREATE INDEX ix_clarifications_delivery
    ON clarifications (status, next_delivery_attempt_at_utc);
CREATE INDEX ix_clarifications_employee
    ON clarifications (employee_telegram_user_id, status);

CREATE TABLE report_deliveries (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_key                  TEXT NOT NULL UNIQUE,
    report_type                 TEXT NOT NULL CHECK (report_type IN ('daily', 'on_demand')),
    variant                     TEXT NOT NULL DEFAULT 'summary',
    manager_telegram_user_id    INTEGER NOT NULL,
    period_start_date           TEXT NOT NULL,
    period_end_date             TEXT NOT NULL,
    scheduled_at_utc            TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'sending', 'sent', 'failed',
                                                  'failed_uncertain', 'skipped')),
    attempts                    INTEGER NOT NULL DEFAULT 0,
    next_attempt_at_utc         TEXT,
    chunk_count                 INTEGER NOT NULL DEFAULT 0,
    chunks_sent                 INTEGER NOT NULL DEFAULT 0,
    telegram_message_ids        TEXT,
    snapshot_json               TEXT,
    narrative_used              INTEGER NOT NULL DEFAULT 0,
    last_error_category         TEXT,
    lease_owner                 TEXT,
    lease_expires_at_utc        TEXT,
    created_at_utc              TEXT NOT NULL,
    updated_at_utc              TEXT NOT NULL,
    sent_at_utc                 TEXT
);

CREATE INDEX ix_report_deliveries_due
    ON report_deliveries (status, next_attempt_at_utc);

CREATE TABLE report_requests (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id           INTEGER UNIQUE REFERENCES source_messages (id) ON DELETE RESTRICT,
    manager_telegram_user_id    INTEGER NOT NULL,
    request_text                TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'received'
                                CHECK (status IN ('received', 'awaiting_period', 'resolved', 'expired')),
    period_kind                 TEXT,
    report_type                 TEXT,
    report_delivery_id          INTEGER REFERENCES report_deliveries (id) ON DELETE SET NULL,
    created_at_utc              TEXT NOT NULL,
    updated_at_utc              TEXT NOT NULL,
    resolved_at_utc             TEXT
);

-- Bot messages that a user can reply to (confirmation, clarification question,
-- report-period question). Enables deterministic reply-target resolution.
CREATE TABLE bot_messages (
    chat_id             INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('event_confirmation', 'clarification_prompt',
                                                      'report_prompt', 'notice')),
    event_id            INTEGER REFERENCES events (id) ON DELETE SET NULL,
    clarification_id    INTEGER REFERENCES clarifications (id) ON DELETE SET NULL,
    report_request_id   INTEGER REFERENCES report_requests (id) ON DELETE SET NULL,
    created_at_utc      TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX ix_bot_messages_event ON bot_messages (event_id);
CREATE INDEX ix_bot_messages_clarification ON bot_messages (clarification_id);
CREATE INDEX ix_bot_messages_report_request ON bot_messages (report_request_id);

-- Audit metadata for AI calls. Deliberately contains no prompt or message text.
CREATE TABLE ai_calls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    operation          TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT,
    outcome            TEXT NOT NULL,
    error_category     TEXT,
    latency_ms         INTEGER,
    source_message_id  INTEGER REFERENCES source_messages (id) ON DELETE SET NULL,
    created_at_utc     TEXT NOT NULL
);
