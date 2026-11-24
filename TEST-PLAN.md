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

## I. Delivery integrity (added in the second pass)

Every item here is automated and passes offline; the "verified defect" column names the
behavior that was wrong before the fix.

- [ ] Two overlapping workers send a private clarification question at most once. (A1)
- [ ] A worker killed between the Telegram call and the bookkeeping does not cause a
      second question when the next run happens. (A2)
- [ ] A timeout / connection reset after the request was written is recorded as an
      unknown outcome, never resent automatically, and stays answerable. (A3)
- [ ] A stale `sending` claim is swept to an unknown outcome instead of being retried. (A2)
- [ ] A live claim is respected: another worker skips it instead of stealing it.
- [ ] A blocked bot / chat-not-found question becomes permanently undeliverable with one
      neutral group hint and no business detail. (A4)
- [ ] Every clarification event is created once, even when the same answer is processed
      twice.
- [ ] A report is frozen before the first chunk is sent; a resumed report never contains
      data that changed after the freeze. (B2)
- [ ] A crash after chunk *n* resumes at chunk *n+1* and never repeats 1..*n*. (B1)
- [ ] A delivery interrupted in an unknown position stops as uncertain and notifies the
      manager exactly once. (B1)
- [ ] A lost or corrupt snapshot never causes confirmed chunks to be resent. (B2)
- [ ] `chunks_sent > len(chunks)` stops for review instead of restarting. (B3)
- [ ] A multi-day outage records one `skipped` row per missed day and never sends a stale
      report. (B4)
- [ ] One raising delivery does not starve the rest of the queue. (B5)
- [ ] A worker that lost its lease cannot finish or requeue another run's update. (D1)
- [ ] A failing phase does not stop the other phases and surfaces in the summary. (D2)
- [ ] `/health` and `scripts/doctor.py` expose how many deliveries need human review,
      without any content.
- [ ] Migrations: a modified historical migration is rejected by checksum; five
      concurrent `ensure_schema` calls apply each version once; a failing migration rolls
      back; 001→002 keeps ids, timestamps and the id sequence; duplicate filenames are
      refused. (D3)
- [ ] The declared Python minimum (3.9) is enforced by parsing every shipped file with
      `feature_version=(3, 9)` and by a 3.10+/3.11+ API denylist. (G1)

## J. Lease fencing and reply resolution (added in the third pass)

- [ ] A worker that lost a report-delivery lease cannot update chunk progress, mark the
      delivery sent, release it, or settle it, and leaves the new owner's lease intact.
- [ ] The retry-exhaustion decision cannot override a delivery another worker just claimed.
- [ ] A stale worker that notices the loss stops sending immediately; no chunk is sent
      twice and the manager receives at most one "outcome unknown" notice for the delivery.
- [ ] The worker that owns the delivery can still finish it normally.
- [ ] An employee who answers a clarification question before the bot has recorded the
      question message is answered, not ignored; the event is confirmed.
- [ ] The same holds when the sending process died and the mapping will never appear
      (`delivery_uncertain`), including after a restart.
- [ ] The answer is applied exactly once: no second revision, no second confirmation, and
      a duplicated Telegram update or a later reply to the same question changes nothing.
- [ ] A crash while applying the answer leaves the reply queued and the event untouched;
      the retry applies it once.
- [ ] A reply from another employee, or a group reply, is never attributed to someone
      else's question; two unconfirmed questions for one employee are never guessed.
- [ ] An expired or never-attempted question cannot be reopened by a late reply.
- [ ] The group still receives nothing from this path.

## H. Human acceptance test

Using a controlled Telegram test group and test accounts after OpenCode has configured a test environment:
1. An employee sends a clear sale message.
2. An employee sends an ordinary unrelated message.
3. An employee sends an ambiguous expense and answers the private clarification.
4. The employee replies to the original message with a correction.
5. The manager requests a date-range report.
6. The manager receives a daily report at the configured time in a test run.

The owner reviews the results before production deployment.

## Executable coverage map (second pass)

| Section | Automated in | Notes |
|---|---|---|
| A. Authorization and routing | `test_authorization.py`, `test_capture_events.py` | Numeric IDs only. |
| B. Webhook and durable queue | `test_webhook.py`, `test_outbox.py`, `test_worker.py` | Includes replay and lease-loss cases. |
| C. Classification and extraction | `test_capture_events.py`, `test_ai_schema.py`, `test_nvidia_adapter.py` | Fake provider only. |
| D. Clarification | `test_clarification.py`, `test_clarification_delivery.py` | Lifecycle, concurrency, crash, uncertainty. |
| E. Corrections and audit | `test_corrections.py` | |
| F. Reports | `test_reporting.py`, `test_reports.py`, `test_report_delivery_safety.py` | Frozen content, per-chunk resume, gap scan. |
| G. cPanel and integrity | `test_integrity.py`, `test_migrations.py`, `test_config.py`, `test_decimal_time.py` | Local only; says nothing about the real account. |
| I. Delivery integrity | `test_clarification_delivery.py`, `test_report_delivery_safety.py`, `test_migrations.py` | |
| J. Lease fencing and reply resolution | `test_report_delivery_lease.py` (11), `test_clarification_reply_race.py` (19) | 30 tests added in the third pass |
| K. Answer claim and state safety | `test_clarification_answer_claim.py` (27) | 27 tests added in the fourth pass; 16 of them fail on `93c1ca7` |
| H. Human acceptance | — | Requires the owner's test group and a deployed instance. |

## Test evidence

Arena reports (fourth pass, executed locally): `python3 -m pytest` → `352 passed in 8.66s`
on Python 3.11.2 / SQLite 3.40.1; the same suite at the starting commit `93c1ca7` was
`325 passed in 8.25s`. The third pass had left 325 passing tests; 27 were added in
`tests/test_clarification_answer_claim.py` and no existing test was modified or relaxed.
The concurrency cases use a `threading.Barrier` inside the AI call and the state cases
use disposable rows — no test sleeps. Nothing in section H was executed. No test reached
Telegram, NVIDIA, cPanel or the accounting system — every external call is a fake.

Fourth-pass coverage in detail:
- Two distinct valid answers, synchronized before the write: exactly one revision, exactly
  one confirmation, one reply `processed`, the other `ignored`/`answer_claim_lost`.
- A stale answer snapshot (the loser's view) applies nothing; duplicate `update_id` still
  short-circuits before the model is called; a non-owner cannot claim or resolve.
- Claim eligibility: `awaiting_answer`/`sending`/`delivery_uncertain` claimable,
  `pending_delivery`/`resolved`/`expired`/`unable_to_deliver` not; a second claim is
  refused; a released claim is claimable again.
- Failure after the claim (revision write raises) rolls the claim back, leaves no revision
  and no confirmation, and the next answer resolves normally; an AI failure before the
  claim leaves the question answerable and the update retryable.
- `delivery_uncertain` is rejected for `resolved`/`answered`/`expired`/`unable_to_deliver`,
  for a stale worker after a takeover, and for every transition called without a worker
  id; it still applies for the current lease owner.
- The real bookkeeping-failure path preserves `resolved`/`expired`/`unable_to_deliver`,
  sends nothing, and never resends; resolve/expire/requeue cannot rewrite `resolved`.

OpenCode must later report:
- Exact test commands.
- Number of passed/failed/skipped tests.
- Any manual tests and their observed results.
- Actual cPanel compatibility findings.
- Known limitations and untested behavior.
- Confirmation that no production service or real user was contacted without approval.
