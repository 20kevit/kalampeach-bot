# Architecture

## 1. System boundary

One standalone application:

`Telegram Bot API → Application Services → Independent Database`

The application calls NVIDIA through a provider adapter for classification, extraction, clarification support, and report narrative generation.

No component may connect to the separate KalamPeach accounting website or its database/API.

## 2. Logical components

1. **Telegram adapter**
   - Receives updates and sends replies/reports.
   - Normalizes Telegram messages into an internal message object.
   - Enforces allowed chat routing before business processing.

2. **Authorization service**
   - Checks numeric Telegram user ID and configured group ID.
   - Resolves employee/manager permissions.

3. **Message processor**
   - Handles idempotency.
   - Stores source message metadata.
   - Routes text to event classification/extraction.
   - Handles replies and clarification responses.

4. **AI provider interface**
   - Exposes narrow operations such as classify/extract and summarize.
   - NVIDIA implementation reads endpoint, model, and key from configuration.
   - Does not own database writes or business decisions.

5. **Domain/event service**
   - Validates event semantics and required fields.
   - Decides save, ignore, pending clarification, or reject.
   - Applies authorized corrections.

6. **Persistence layer**
   - Owns schema, transactions, event records, revisions, processed updates, report delivery records, and pending clarifications.
   - Database is the source of truth.

7. **Reporting service**
   - Queries confirmed records for a period.
   - Computes totals deterministically.
   - Passes only computed facts to AI for narrative formatting.
   - Enforces manager-only access.

8. **Scheduler**
   - Triggers daily report at 23:30 Asia/Tehran.
   - Uses persistent delivery state and idempotency key per manager/date/report type.

## 3. Message flow

### Clear business message
1. Receive Telegram update.
2. Validate chat and sender.
3. Check update idempotency.
4. Persist source message.
5. Classify and extract via AI.
6. Validate strict output schema and business rules.
7. In one transaction, create event and mark update processed.
8. Send a short confirmation if appropriate.

### Irrelevant message
1. Validate sender and chat.
2. Persist minimum processing metadata according to retention policy.
3. Classify as irrelevant.
4. Mark update processed; create no business event and send no group response.

### Ambiguous message
1. Persist source and pending candidate.
2. Create clarification request with the missing fields.
3. Send private question to the original author.
4. Link the reply to the pending clarification.
5. Re-validate and confirm only when sufficient information exists.

### Correction
1. Receive reply to a source message.
2. Resolve source Telegram message to event.
3. Check author/manager permission.
4. Ask AI to interpret the requested change against current event state.
5. Validate patch and semantic consistency.
6. Append revision and update current projection transactionally.

### Report
1. Authenticate manager.
2. Parse requested period/type.
3. Query confirmed events for period.
4. Aggregate with deterministic code, separated by currency and category.
5. Build a fact payload including coverage and pending counts.
6. Optionally ask AI for Persian narrative using only supplied facts.
7. Validate narrative response and send report.
8. Persist report request/delivery state.

## 4. Persistence choice

Use a simple relational database suitable for one bot instance and low operational complexity. SQLite is the preferred MVP default unless the selected runtime or deployment constraints provide a documented reason not to use it.

- Enable safe transaction behavior and appropriate journal mode.
- Use foreign keys and uniqueness constraints.
- Keep database file outside ephemeral build/container layers.
- Include schema versioning/migrations.
- Do not use an in-memory database for real operation.
- Do not introduce a separate queue service for MVP; use durable database state and a simple worker loop if asynchronous work is needed.

## 5. Runtime model

A single process is preferred initially if the selected framework supports reliable Telegram polling/webhook handling and timezone-aware scheduling. If multiple processes are required, ensure one scheduler leader and database-safe idempotency.

The system must recover pending clarifications and due/failed reports after restart.

## 6. Reliability principles

- Telegram update uniqueness prevents duplicate processing.
- Event creation and update completion must be transactionally consistent.
- AI calls happen outside long-running database transactions.
- External API failures never produce fabricated events.
- Report delivery uses a unique period key and explicit states.
- Retries are bounded and observable.
- No automatic destructive database repair or reset.
