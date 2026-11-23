# Changelog

All notable project changes are recorded here.

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
- **Tests**: 249 offline tests covering TEST-PLAN sections A–G.

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
