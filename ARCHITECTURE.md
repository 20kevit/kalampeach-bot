# Architecture

## 1. System boundary

One standalone Python application:

`Telegram Bot API → HTTPS Webhook (cPanel WSGI app) → Durable Update Inbox → Cron Worker → Application Services → Independent Database`

The application calls NVIDIA through a provider adapter for classification, extraction, clarification support, and report narrative generation.

No component may connect to the separate KalamPeach accounting website or its database/API.

## 2. Logical components

1. **WSGI web entry point**
   - Runs through the Python application facility provided by cPanel (commonly Passenger).
   - Exposes a small HTTPS webhook endpoint.
   - Does not rely on an always-running polling loop or in-process scheduler.

2. **Telegram webhook adapter**
   - Validates Telegram's configured webhook secret-token header.
   - Normalizes updates.
   - Durably stores each update in an inbox with a unique Telegram update ID.
   - Returns promptly after durable acceptance; expensive AI work is deferred to cron worker.

3. **Authorization service**
   - Checks numeric Telegram user ID and configured group ID.
   - Resolves employee/manager permissions before AI processing.

4. **Cron worker**
   - Invoked by cPanel Cron Jobs, preferably once per minute.
   - Claims queued updates safely, processes them, retries transient failures with bounded backoff, and runs due report jobs.
   - Uses database locks/leases and idempotency so overlapping cron invocations cannot double-process events or reports.
   - Must be safe to exit after each invocation; no daemon is assumed.

5. **Message processor**
   - Stores source message metadata.
   - Routes authorized text to event classification/extraction.
   - Handles replies and clarification responses.

6. **AI provider interface**
   - Exposes narrow operations such as classify/extract and summarize.
   - NVIDIA implementation reads endpoint, model, and key from configuration.
   - Does not own database writes or business decisions.

7. **Domain/event service**
   - Validates event semantics and required fields.
   - Decides save, ignore, pending clarification, or reject.
   - Applies authorized corrections.

8. **Persistence layer**
   - Owns schema, transactions, event records, revisions, processed updates, report delivery records, and pending clarifications.
   - Database is the source of truth.

9. **Reporting service**
   - Queries confirmed records for a period.
   - Computes totals deterministically.
   - Passes only computed facts to AI for narrative formatting.
   - Enforces manager-only access.

## 3. Message flow

### Incoming Telegram update
1. Telegram sends an HTTPS POST to the configured webhook URL.
2. WSGI endpoint validates the secret token header and payload.
3. In a short transaction, persist the raw update/inbox row using unique `update_id`.
4. Return HTTP success promptly once durable storage succeeds.
5. cPanel cron invokes the worker; the worker claims and processes queued updates.

### Clear business message
1. Validate chat and sender.
2. Check update idempotency.
3. Persist source message.
4. Classify and extract via AI.
5. Validate strict output schema and business rules.
6. In one transaction, create event and mark update processed.
7. Send a short confirmation if appropriate.

### Irrelevant message
1. Validate sender and chat.
2. Persist minimum processing metadata according to retention policy.
3. Classify as irrelevant.
4. Mark update processed; create no business event and send no group response.

### Ambiguous message
1. Persist source and pending candidate.
2. Create clarification request with the missing fields.
3. Worker sends private question to the original author.
4. Link the employee response to the pending clarification.
5. Re-validate and confirm only when sufficient information exists.

### Correction
1. Worker receives a reply update to a source message.
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

Use a simple relational database suitable for one bot instance and low operational complexity. SQLite is the preferred MVP default unless the actual cPanel host does not support it reliably.

- Enable foreign keys and safe transaction behavior.
- Use uniqueness constraints and short transactions.
- Keep the database file in a durable, non-public directory under the hosting account home directory, outside the public web root and deployment upload path.
- Include schema versioning/migrations.
- Do not use an in-memory database for real operation.
- Do not introduce Redis, Celery, or a separate queue service for MVP; the durable database inbox is the queue.

## 5. Runtime model

- Web requests run through cPanel's supported Python WSGI application mechanism.
- Telegram uses webhook delivery over HTTPS; production long polling is not supported as the deployment model.
- A cPanel cron job invokes a short-lived Python worker, ideally once per minute.
- A separate cron invocation may run backups only if configured and approved; backups must not be implemented as an unbounded web request.
- Do not rely on in-process timers, APScheduler, background threads, or persistent daemon processes for critical work.
- The worker must safely exit after processing a bounded batch and be safe to invoke again.
- The system must recover queued updates, pending clarifications, and due/failed reports after a hosting restart.

## 6. Reliability principles

- Telegram update uniqueness prevents duplicate processing.
- Webhook acknowledges only after durable inbox persistence.
- Worker claims use a database-safe lease/lock with expiration.
- Event creation and update completion must be transactionally consistent.
- AI calls happen outside long-running database transactions.
- External API failures never produce fabricated events.
- Report delivery uses a unique period key and explicit states.
- Cron overlap and retries cannot duplicate business events or daily reports.
- No automatic destructive database repair or reset.
