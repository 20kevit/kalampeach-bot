-- KalamPeach Telegram Operations Bot — initial schema (version 1).
-- Append-only migration strategy: never edit this file after deployment.
-- All timestamps are ISO-8601 UTC strings ending in "Z".
-- Money and quantities are canonical decimal strings, never floats.

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at_utc TEXT NOT NULL
);

-- Observed Telegram identities. Authorization itself comes from configuration
-- (numeric IDs); this table is an audit aid and records what was observed.
CREATE TABLE users (
    telegram_user_id INTEGER PRIMARY KEY,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'employee',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

-- Durable webhook inbox: the queue itself. update_id is the idempotency key
-- Telegram provides, so duplicate webhook deliveries cannot double-process.
CREATE TABLE telegram_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER NOT NULL UNIQUE,
    received_at_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    kind TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT,
    claim_token TEXT,
    claimed_until_utc TEXT,
    available_at_utc TEXT NOT NULL,
    processed_at_utc TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX idx_inbox_due ON telegram_inbox(status, available_at_utc);
CREATE INDEX idx_inbox_claim ON telegram_inbox(claim_token);

-- Original Telegram messages, preserved for provenance and reply resolution.
CREATE TABLE source_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_update_id INTEGER,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    reply_to_message_id INTEGER,
    sender_telegram_user_id INTEGER,
    chat_type TEXT NOT NULL,
    message_text TEXT,
    message_timestamp_utc TEXT,
    processing_status TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (chat_id, message_id)
);

CREATE INDEX idx_source_reply_lookup ON source_messages(chat_id, message_id);
CREATE INDEX idx_source_sender ON source_messages(sender_telegram_user_id);

-- The current projection of each business event. History lives in
-- event_revisions; a correction never overwrites history.
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Nullable only while a pending candidate's category is still unknown;
    -- a confirmed event always has one of the four supported event types.
    event_type TEXT,
    status TEXT NOT NULL,
    event_date TEXT,
    amount TEXT,
    currency TEXT,
    item_name TEXT,
    quantity TEXT,
    unit TEXT,
    inventory_direction TEXT,
    description TEXT,
    notes TEXT,
    details_json TEXT,
    created_by_telegram_user_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL UNIQUE REFERENCES source_messages(id),
    current_revision_number INTEGER NOT NULL DEFAULT 1,
    confirmed_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX idx_events_period ON events(event_date, status);
CREATE INDEX idx_events_confirmed ON events(status, confirmed_at_utc);

-- Private clarification requests sent to the original author.
CREATE TABLE clarifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    employee_telegram_user_id INTEGER NOT NULL,
    question_text TEXT NOT NULL,
    missing_fields_json TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_chat_id INTEGER,
    prompt_message_id INTEGER,
    prompt_source_message_id INTEGER REFERENCES source_messages(id),
    response_source_message_id INTEGER REFERENCES source_messages(id),
    round INTEGER NOT NULL DEFAULT 1,
    last_error_category TEXT,
    created_at_utc TEXT NOT NULL,
    resolved_at_utc TEXT
);

CREATE INDEX idx_clar_open ON clarifications(employee_telegram_user_id, status);
CREATE INDEX idx_clar_prompt ON clarifications(prompt_chat_id, prompt_message_id);
CREATE INDEX idx_clar_event ON clarifications(event_id);

-- Append-only revision history. previous/new snapshots are JSON documents.
CREATE TABLE event_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    revision_number INTEGER NOT NULL,
    actor_telegram_user_id INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    source_message_id INTEGER REFERENCES source_messages(id),
    clarification_id INTEGER REFERENCES clarifications(id),
    reason TEXT,
    previous_snapshot_json TEXT,
    new_snapshot_json TEXT NOT NULL,
    validation_status TEXT NOT NULL DEFAULT 'accepted',
    UNIQUE (event_id, revision_number),
    UNIQUE (event_id, source_message_id)
);

CREATE INDEX idx_revisions_event ON event_revisions(event_id, revision_number);

-- Daily and on-demand report delivery state. report_key is the idempotency
-- key that prevents a duplicate report after a restart or overlapping cron.
CREATE TABLE report_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_key TEXT NOT NULL UNIQUE,
    report_type TEXT NOT NULL,
    manager_telegram_user_id INTEGER NOT NULL,
    period_start_date TEXT NOT NULL,
    period_end_date TEXT NOT NULL,
    status TEXT NOT NULL,
    fact_snapshot_json TEXT,
    narrative_source TEXT,
    telegram_message_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT,
    claim_token TEXT,
    claimed_until_utc TEXT,
    next_attempt_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    sent_at_utc TEXT
);

CREATE INDEX idx_report_due ON report_deliveries(status, next_attempt_at_utc);
CREATE INDEX idx_report_period ON report_deliveries(report_type, period_end_date);
