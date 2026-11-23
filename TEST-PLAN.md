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

## Test implementation mapping (Arena, code-ready phase)

Every acceptance area above is covered by executable pytest tests. Tests run fully
offline: the AI provider and Telegram client are fakes, the database is a temporary
SQLite file, and the clock is fixed. No credentials, no network, no live chat.

| Area | Test file | What it proves |
|---|---|---|
| A. Authorization and routing | `tests/test_authorization.py` | numeric-ID authorization, group isolation, unauthorized senders never reach the AI, bot/service messages ignored, manager-only reports, username changes irrelevant, manager recording flag |
| B. Webhook and durable queue | `tests/test_webhook.py`, `tests/test_worker.py` | secret accepted/rejected, durable persistence before ack, duplicate `update_id`, prompt ack without AI, claim/lease overlap, expired-claim recovery, bounded retries, exhausted claims observable, batch bounds, claim-token enforcement |
| C. Classification and extraction | `tests/test_capture.py` | four event types, ordinary chat ignored, ambiguous fields → pending + private question, unknown ≠ zero, currency preservation and default policy, malformed AI output rejected, AI outage preserves source and retries, duplicate updates |
| D. Clarification | `tests/test_clarification.py` | private question only, answer resolution via reply or single open item, follow-up rounds, round limit → rejected, undeliverable private chat handling, no group leaks |
| E. Corrections and audit | `tests/test_correction.py` | reply target resolution, author/manager permissions, another employee denied, revision append with previous snapshot, duplicate correction idempotency, invalid semantic correction refused, multiple-event target disambiguation |
| F. Reports | `tests/test_reports.py` | manager-only, 23:30 Tehran schedule and cutoff, missed-tick recovery, late flag, sales/expense separation, currency grouping, exact decimals, pending excluded and disclosed, empty-day wording, AI narrative guard, template fallback, delivery idempotency, `uncertain` handling, on-demand periods/ranges |
| G. cPanel compatibility and data integrity | `tests/test_schema_migrations.py`, `tests/test_domain.py`, `tests/test_independence.py`, `tests/test_logging.py`, `tests/test_ops_scripts.py`, `tests/test_config.py` | Passenger entry point smoke test, no long polling/daemon deps, bounded worker, schema migrations preserve data, foreign keys, decimal-safe money, UTC storage, secret-free repository and logs, backup/restore drill, fail-closed startup |
| H. Human acceptance test | not automated | requires approved test accounts; executed only by OpenCode with the owner |

Additional coverage: `tests/test_nvidia_provider.py` (wire format, fenced JSON, one
repair attempt, retries, timeouts, auth errors, prompt-delimiter neutralisation,
schema rejection) and `tests/test_ops_scripts.py` (real CLI runs of `init_db.py`,
`run_worker.py`, `verify_setup.py`, `backup_db.py`, `report_admin.py`).

### Local execution evidence (Arena)

- Command: `python -m pytest -q` inside a local virtualenv.
- Environment: CPython **3.11.2**, Linux, offline (no Telegram/NVIDIA calls, no
  credentials), dependencies installed from `requirements-dev.txt`.
- Result at handoff: **192 passed, 0 failed, 0 skipped**.
- Also run: `ruff check app scripts tests passenger_wsgi.py --select E,F,W,B` → clean.

### Not executed (explicitly not claimed)

- Any run on the real cPanel host, Passenger, or its Python build.
- Live webhook registration or Telegram Bot API calls.
- Live NVIDIA API calls (no key/model exists in this repository).
- Performance/load, real filesystem locking under concurrent cron on the host, or
  quota/limits behaviour.
- The human acceptance test (§H).

OpenCode must re-run the suite on the host and report actual counts, plus the manual
acceptance results.
