# Data Model

The schema below is a logical contract, not a mandate for exact SQL names. Keep the physical schema minimal while preserving every invariant.

## 1. Core entities

### users
- `telegram_user_id` — unique numeric Telegram ID; primary identity.
- `display_name` — optional observed display name.
- `role` — employee or manager.
- `is_active` — whether access is currently allowed.
- `created_at`, `updated_at`.

Authorization must not depend on username/display name.

### source_messages
- Internal ID.
- `telegram_update_id` when available.
- `chat_id`, `message_id`, optional `reply_to_message_id`.
- `sender_telegram_user_id`.
- `message_text` (original text).
- `message_timestamp_utc`.
- `chat_type` (configured group or private).
- `processing_status` (received, processed, ignored, pending, failed).
- `created_at`.

Uniqueness constraints must prevent duplicate Telegram updates and duplicate source-message processing.

### events
- Internal stable event ID.
- `event_type`: sale_revenue, expense_purchase, inventory_movement, operational_note.
- `status`: confirmed, pending_clarification, rejected.
- `event_date` — business date interpreted in Asia/Tehran.
- `amount` — nullable decimal representation; never binary floating point for money.
- `currency` — explicit currency code/label; nullable only while unresolved.
- `item_name` — optional.
- `quantity` — nullable decimal.
- `unit` — optional.
- `description` / `notes` — concise extracted facts.
- `created_by_telegram_user_id`.
- `source_message_id` — FK to source_messages.
- `created_at_utc`, `updated_at_utc`.
- `current_revision_number`.

Do not force irrelevant fields to zero. Nullable means unknown/not applicable.

### event_revisions
Append-only history:
- Revision ID and event ID.
- Revision number.
- Actor Telegram user ID.
- Timestamp UTC.
- Source message ID or clarification ID.
- Change reason/source text.
- Previous structured snapshot.
- New structured snapshot.
- Validation/approval status.

Never delete or overwrite prior revision rows.

### clarifications
- ID and event ID.
- Original employee Telegram ID.
- Question text and missing field identifiers.
- Status: open, answered, resolved, expired, unable_to_deliver.
- Prompt source message ID and response source message ID.
- Created/resolved timestamps.

### processed_updates
If not represented directly by source_messages:
- Telegram update ID unique.
- Processing status.
- Attempt count and last error category.
- Created/updated timestamps.

Avoid duplicate tables if source_messages already provides the required durable idempotency invariant.

### report_deliveries
- Report key (unique per report type, manager, and local date/period).
- Report type and period start/end.
- Manager Telegram ID.
- Status: pending, sent, failed.
- Computed data snapshot or reproducible query metadata.
- Delivery message ID when successful.
- Attempt count, last error category, timestamps.

## 2. Event payload rules

Keep fields normalized where they are needed for filtering and aggregation. Optional category-specific details may be stored in a validated JSON payload, but money, currency, event date, quantity, unit, and event type must remain queryable.

For a sale/revenue event:
- Amount and currency are required to include it in a financial total.
- Description may explain the sale.
- Missing currency must trigger configured default resolution or clarification.

For an expense/purchase event:
- Amount and currency are required for financial total.
- Keep expense/purchase distinct from sale/revenue.

For inventory:
- Item, quantity, and unit are required to report a quantified movement.
- Movement direction must distinguish received, consumed, wasted, and adjustment.
- Do not infer monetary value from quantity.

For operational note:
- A clear description is required.
- Financial fields are normally null.

## 3. Referential and audit invariants

- Every event has a source message.
- Every source message records sender, chat, message ID, and source timestamp.
- A correction must create an event_revision.
- A reply correction must resolve to a source message in the same Telegram chat.
- Only authorized actors may create or revise events.
- Deactivation of a user must not erase their historical events.
- All money calculations use decimal-safe arithmetic.
- All stored timestamps are UTC; business dates use Asia/Tehran.
- A report must be traceable to the records and query period used to produce it.

## 4. Retention and privacy

Do not retain more Telegram content than needed for auditability and event provenance. Do not log full private messages by default. The owner must set a retention policy before production if legal/business requirements demand one. Do not automatically delete historical records in MVP.
