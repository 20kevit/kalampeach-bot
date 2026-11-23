# Test Plan and Acceptance Criteria

All mandatory criteria must pass before the MVP is considered complete. Arena must author tests in GitHub; OpenCode will later execute them in the supported environment and report actual results.

## A. Authorization and routing

- [ ] Authorized employee message in configured group is processed.
- [ ] Authorized employee private text is processed.
- [ ] Unauthorized user cannot create or edit events.
- [ ] Unauthorized message is not sent to NVIDIA.
- [ ] Message from an unconfigured group is ignored.
- [ ] Manager-only report request is denied to ordinary employees.
- [ ] Username changes do not affect numeric-ID authorization.
- [ ] Bot messages and Telegram service messages do not become business events.

## B. Webhook and durable queue

- [ ] Valid Telegram webhook secret token is accepted.
- [ ] Missing or incorrect webhook secret is rejected.
- [ ] Webhook persists update before acknowledging success.
- [ ] Duplicate update ID is acknowledged safely without duplicate inbox rows.
- [ ] Webhook returns promptly and does not wait for NVIDIA processing.
- [ ] Worker processes queued updates and marks completion durably.
- [ ] Worker recovers expired claims after interruption.
- [ ] Concurrent/overlapping cron invocations cannot process one update twice.
- [ ] Transient failures use bounded retries; permanent failures remain observable.

## C. Classification and extraction

- [ ] Clear Persian sale/revenue message creates one correct event.
- [ ] Clear expense/purchase message creates one correct event.
- [ ] Clear inventory movement preserves item, quantity, unit, and direction.
- [ ] Clear operational incident creates a note event.
- [ ] Ordinary conversation creates no event.
- [ ] Ambiguous required amount/date/item does not create a confirmed event.
- [ ] Unknown amount is not converted to zero.
- [ ] Currency is preserved or resolved only through configured default.
- [ ] AI malformed JSON is rejected safely.
- [ ] AI timeout/outage preserves source message and creates no fabricated event.
- [ ] Duplicate Telegram update creates at most one event.

## D. Clarification

- [ ] Ambiguous group event triggers private question to original author.
- [ ] No private business details are posted in group.
- [ ] Employee answer resolves only the linked pending clarification.
- [ ] Employee who has not started bot is handled without exposing details.
- [ ] Unresolved clarification remains visible as pending and is not included in confirmed totals.

## E. Corrections and audit

- [ ] Reply to source message resolves the correct event.
- [ ] Author can correct own event.
- [ ] Another employee cannot correct that event.
- [ ] Manager can correct any event.
- [ ] Each accepted correction appends a revision.
- [ ] Previous revision remains retrievable.
- [ ] Ambiguous/multiple reply targets require clarification.
- [ ] Duplicate correction update is idempotent.

## F. Reports

- [ ] Only manager can request reports.
- [ ] Daily report is scheduled for 23:30 Asia/Tehran through cron-driven due-job processing.
- [ ] Report date boundaries use Asia/Tehran, not server/cron timezone.
- [ ] A worker run after a missed 23:30 tick recovers the due report once.
- [ ] Sales and expenses are grouped separately.
- [ ] Different currencies are never added together.
- [ ] Pending events are excluded from confirmed totals and disclosed.
- [ ] Empty day says no events were recorded, not that sales were zero.
- [ ] AI narrative cannot change computed figures.
- [ ] Deterministic fallback report works when AI is unavailable.
- [ ] Cron restart/overlap does not lose or duplicate a due report.
- [ ] Report idempotency prevents duplicate delivery under normal retry/restart cases.

## G. cPanel compatibility and data integrity

- [ ] Application imports and serves through the selected cPanel WSGI/Passenger entry point.
- [ ] No production dependency on long polling, daemon processes, systemd, Docker, Redis, or root access.
- [ ] Cron worker exits cleanly after a bounded batch.
- [ ] Database path is durable and outside public web root.
- [ ] SQLite works on the selected host or a documented minimal supported fallback is used.
- [ ] Database is independent of the separate accounting website.
- [ ] Schema initialization/migrations preserve existing records.
- [ ] Event and revision writes are transactional.
- [ ] Money uses decimal-safe storage and arithmetic.
- [ ] Timestamps are stored in UTC and business dates use Asia/Tehran.
- [ ] Secrets are absent from repository and logs.
- [ ] Backup and restore procedure is tested.
- [ ] Startup fails clearly when required configuration is missing.
- [ ] Logs identify failures without exposing full private message content or credentials.

## H. Human acceptance test

Using a controlled Telegram test group and test accounts after OpenCode has configured a test environment:
1. An employee sends a clear sale message.
2. An employee sends an ordinary unrelated message.
3. An employee sends an ambiguous expense and answers the private clarification.
4. The employee replies to the original message with a correction.
5. The manager requests a date-range report.
6. The manager receives a daily report at the configured time in a test run.

The owner reviews the results before production deployment.

## Test evidence

Arena must report which tests it authored and which it could not execute. OpenCode must later report:
- Exact test commands.
- Number of passed/failed/skipped tests.
- Any manual tests and their observed results.
- Actual cPanel compatibility findings.
- Known limitations and untested behavior.
- Confirmation that no production service or real user was contacted without approval.
