# Changelog

All notable project changes are recorded here.

## [0.2.0] — Reliability pass (unreleased, not deployed)

Second development pass on `arena/c55428c7-kalampeach-bot`. The baseline is commit
`8d3f93a` ("Implement KalamPeach Telegram operations bot MVP"), branched from `860243e`.
Scope: the delivery paths that decide *whether a human ever sees a question or a
report*. Each defect below was reproduced from the code before it was fixed, and each
fix has a regression test that fails without it. Nothing has been deployed; no live
Telegram, NVIDIA, cPanel or accounting call was made.

### Fixed — clarification delivery (A)

| ID | Verified defect | Fix |
|---|---|---|
| A1 | Two overlapping cron runs could both send the same private question: the row was only marked after Telegram returned, and the due query had no claim. | `pending_delivery` → `sending` claim with `lease_owner`/`lease_expires_at_utc` and `BEGIN IMMEDIATE`; the second run sees the claim and skips. Verified with four concurrent threads: exactly one send. |
| A2 | A process killed between the Telegram call and the bookkeeping left the row looking untouched, so the next run asked the same question again. | The claim is committed *before* the call. A stale `sending` row is swept to `delivery_uncertain` and never resent automatically. Verified by crashing the bookkeeping step after a successful send: still one send. |
| A3 | A timeout / connection reset after the request was written was classified as retryable, so the bot blindly resent a question that may already have arrived. | `TelegramError.uncertain` now maps to `delivery_uncertain`: no resend, no group hint, question stays answerable so the employee can still complete the event by reply. |
| A4 | `delivery_attempts` and `question_count` were incremented on different paths, so backoff and the question counter disagreed with reality. | `delivery_attempts` increments exactly once, at claim; `question_count` increments exactly once, on confirmed delivery. |
| A5 | `delivery_uncertain` rows were invisible to the answer handler and to open-question counts, so an employee who did receive the question could never resolve the event. | `delivery_uncertain` (and a `sending` row whose lease expired) accept an answer; open-question counts include it. |

### Fixed — report delivery (B)

| ID | Verified defect | Fix |
|---|---|---|
| B1 | A delivery left in `sending` by a dead worker was retried from chunk 0, resending chunks the manager already had. | Interrupted deliveries are resolved first: all chunks confirmed → `sent` (recovered); nothing attempted → safe restart; some chunks in flight → `failed_uncertain` plus one manager notice. |
| B2 | If the snapshot was unreadable after a partial send, the report was rebuilt from current data and resent — the manager could receive two different versions of one day. | The snapshot is frozen before the first send and is never rebuilt once a chunk is confirmed; the delivery stops as `failed_uncertain` instead. A resumed report keeps the frozen content even if events are edited afterwards. |
| B3 | `chunks_sent > len(chunks)` was silently reset to 0, which resent the whole report. | Recorded as `delivery_progress_inconsistent` and stopped for human review. |
| B4 | After a multi-day outage only one missed day was recorded, and an old day inside the recovery window could still be sent late. | A bounded 30-day gap scan records one `skipped` row per missed day (visible in operator review) and never sends a stale daily report; only the newest day inside the recovery window is delivered. |
| B5 | One raising delivery aborted the loop, starving every later delivery in the same run. | Per-delivery isolation: the failing row stays due, the rest proceed, the run records `errored`. |

### Fixed — worker, migrations and honesty (D, C, G)

| ID | Verified defect | Fix |
|---|---|---|
| D1 | `finish_update` / `schedule_update_retry` were keyed by `update_id` only, so a worker that woke up after its lease was taken over overwrote the newer run's result. | Both writes are guarded by `lease_owner`; a lost claim changes 0 rows and is reported as `lease_lost` in the run summary. |
| D2 | A failure in the clarification phase aborted the run before the report phase, so a broken question could delay a daily report indefinitely. | The three phases are isolated; failures appear in `phase_failures` and make the cron run exit non-zero. |
| D3 | No test proved that two overlapping migrations are safe, that a modified historical migration is rejected, or that 001→002 preserves data. | `tests/test_migrations.py` covers checksum drift, five concurrent `ensure_schema` calls, rollback of a failing migration, duplicate/invalid filenames, FK enforcement, WAL, and an in-place 001→002 upgrade that keeps ids, timestamps and the AUTOINCREMENT sequence. |
| G1 | `tests/test_integrity.py` imported `tomllib` (Python 3.11+) while `pyproject.toml` declares `>=3.9`. | The test now reads the version with a regex, and two new integrity tests parse every shipped file with `ast.parse(feature_version=(3, 9))` and reject 3.10/3.11-only stdlib APIs. The declared minimum is now enforced rather than asserted. |

### Added

- `kalampeach/migrations/sql/002_clarification_delivery_states.sql` — schema version 2:
  `sending` and `delivery_uncertain` states, `lease_owner`, `lease_expires_at_utc`,
  indexes for due/lease/employee lookups. Additive in meaning; rows, ids and timestamps
  are preserved.
- `GET /health` → `review_needed` block (`clarifications_uncertain`,
  `reports_uncertain`, `total`) so an operator can see that a delivery needs a human
  without any business content being exposed.
- `scripts/doctor.py` → `delivery_review_needed` check (PASS when zero, WARN otherwise).
- Operator-facing log events: `clarification_delivery_uncertain`,
  `clarification_delivery_claim_lost`, `report_delivery_uncertain`,
  `report_delivery_recovered`, `report_delivery_error`, `worker_phase_failed`.
- `OPENCODE-HANDOFF.md`: what is implemented locally, what needs a read-only cPanel
  inspection, what only the owner can supply, what needs explicit approval, and the
  exact command list for install, migration, health, cron, webhook, acceptance,
  backup and restore.
- Tests: 45 new (`test_clarification_delivery.py` 16, `test_report_delivery_safety.py` 11,
  `test_migrations.py` 9, plus additions in `test_worker.py`, `test_reports.py`,
  `test_integrity.py`).

### Changed

- Clarification answers are accepted for `awaiting_answer`, `delivery_uncertain`, and
  `sending` only after its lease expired — never while a live worker is mid-send.
- `ensure_report_delivery` returns `(row, created)`; report delivery rows are created in
  the same transaction that claims the period, so a duplicate row is impossible.
- `scripts/run_worker.py` exits `1` when a phase failed during the run (the summary is
  still printed).
- `scripts/telegram_webhook.py info` now prints a whitelist of the documented
  `getWebhookInfo` fields instead of echoing the whole API response.

### Verified locally (executed in this repository, not on the host)

| Command | Result |
|---|---|
| `python3 -m pytest` | `295 passed in 6.56s` |
| `python3 -m compileall -q kalampeach scripts passenger_wsgi.py` | exit 0 |
| `python3 scripts/migrate.py --status` (fresh DB) | `001 … PENDING`, `002 … PENDING` |
| `python3 scripts/migrate.py --apply` | `applied 001 …`, `applied 002 …` |
| `python3 scripts/doctor.py` (complete config) | `Result: 0 failure(s), 0 warning(s)`, incl. `delivery_review_needed — none` |
| `python3 scripts/run_worker.py` (empty queue) | `{"claimed": 0, …, "phase_failures": {}, "schema_version": 2}`, exit 0 |
| `python3 scripts/backup_db.py --output …` | `backup_created … integrity=ok`, mode `0600` |

Environment used: Python 3.11.2, SQLite 3.40.1, pytest 9.2.0, `Asia/Tehran` from the
system tz database. These results say nothing about the cPanel account, and the suite
does not execute against a real Telegram bot or the NVIDIA API.

## [0.1.0] — MVP implementation (unreleased, not deployed)

Application code, migrations, operational scripts and the offline test suite now exist
for the scope defined in `SCOPE.md`. Nothing has been deployed, and no live Telegram,
NVIDIA, cPanel or accounting-system call has been made from this repository.

### Added

- **Runtime**: dependency-free WSGI application (`kalampeach/web/app.py`) exposed through
  `passenger_wsgi.py`, with `POST <webhook_path>` and `GET|HEAD /health`.
- **Durable intake**: `telegram_updates` inbox keyed by `update_id`; the webhook persists
  the raw update, then returns `200 ok` without calling the AI provider.
- **Cron worker** (`scripts/run_worker.py`): idempotent claims with leases, bounded batch
  size and runtime, exponential backoff (`60 * 2^(n-1)`, capped at 1800 s), max attempts,
  expired-lease recovery, and overdue-day recovery for the daily report.
- **Capture workflows**: sale/revenue, expense/purchase, inventory movement, operational
  note; unknown amount/currency/unit never becomes zero or a guess.
- **Clarification**: private question to the original author only, one narrower follow-up,
  then an honest "still pending" status; blocked-bot delivery is surfaced without leaking
  business details into the group.
- **Corrections**: reply-based, permission-checked (author or manager), revalidated, and
  stored as append-only `event_revisions` rows with previous/new snapshots.
- **Reporting**: deterministic totals grouped per currency (never summed across
  currencies), separate sales/expenses/inventory/incident sections, pending counts,
  empty-day wording, 4096-character chunking, per-chunk resume, and uncertain-delivery
  protection.
- **AI contract**: strict JSON schema validation, one repair retry, provider adapter for
  NVIDIA with configurable base URL/model/timeout, and narrative output that is rejected
  whenever it contains figures or profit claims not present in the computed facts.
- **Database**: versioned SQL migration (`001_initial_schema`), WAL journal, foreign keys,
  UTC timestamps, Tehran business dates, decimal strings for money/quantity, and
  `0600` file permissions.
- **Operations**: `scripts/migrate.py`, `scripts/doctor.py`, `scripts/backup_db.py`,
  `scripts/telegram_webhook.py` (requires explicit `--yes`), plus `.env.example`.
- **Tests**: 249 offline tests covering TEST-PLAN sections A–G (250 after the
  addition recorded in 0.2.0; superseded by the 295 tests listed there).

### Changed

- `/health` reports `503 degraded` until the shipped schema version is applied, instead
  of returning `ok` for an unmigrated database.
- Clarification answers, expiry notices and correction acknowledgements are always sent
  to the employee's private chat, never to the group.
- Backup restore stays manual and documented; the backup script only writes verified
  copies and never overwrites the live database.

### Verified locally (executed in this repository, not on the host)

| Command | Result |
|---|---|
| `python3 -m pytest` | `249 passed in 4.50s` |
| `python3 scripts/migrate.py --status` (fresh DB) | `001 initial_schema   PENDING` |
| `python3 scripts/migrate.py --apply` | `applied 001 initial_schema` |
| `python3 scripts/doctor.py` | `Result: 0 failure(s), 0 warning(s)` |
| `python3 scripts/run_worker.py` (empty queue) | `{"claimed": 0, ..., "schema_version": 1}` |
| `python3 scripts/backup_db.py --output …` | `backup_created … integrity=ok`, mode `0600` |

Environment used: Python 3.11.2, SQLite 3.40.1, pytest 9.2.0, `Asia/Tehran` available
from the system tz database. These results say nothing about the cPanel account.

### Not done here (OpenCode)

- cPanel Python application creation, dependency verification and environment variables.
- Production migration, webhook registration, cron configuration and HTTPS/DNS checks.
- Any message exchanged with a real Telegram user or group.
- TEST-PLAN section H (human acceptance) with real accounts.
