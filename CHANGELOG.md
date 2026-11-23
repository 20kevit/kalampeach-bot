# Changelog

All notable project changes are recorded here.

## [Unreleased]

_No changes yet after 0.1.0._

## [0.1.0] — MVP implementation handoff (code-ready, not deployed)

Arena implemented the complete application, migrations, tests, and documentation in GitHub. Nothing was deployed; no cPanel, webhook, cron, or Telegram/NVIDIA account was touched. See `OPENCODE-HANDOFF.md`.

### Added

- **Configuration** (`app/config.py`): strict validation of every owner-supplied value, private env-file support for cron, fail-closed startup with key names only (no values, no invented defaults).
- **WSGI webhook** (`app/wsgi.py`, `passenger_wsgi.py`): `POST /telegram/webhook` with constant-time `X-Telegram-Bot-Api-Secret-Token` validation, durable enqueue keyed by `update_id`, prompt acknowledgement, `/healthz`, 503 while the schema is uninitialised. No AI call in the request path.
- **Database** (`app/db.py`, `app/migrations/0001_initial.sql`): SQLite with foreign keys, WAL-when-supported, short `BEGIN IMMEDIATE` transactions, versioned append-only migrations (`schema_migrations`), tables for users, inbox, source messages, events, clarifications, revisions and report deliveries.
- **Domain rules** (`app/domain/events.py`, `app/money.py`): four event categories, per-category required fields, decimal-string money/quantity with `Decimal` arithmetic, currency preservation, date plausibility window, semantic validation, revision snapshots.
- **AI layer** (`app/ai/`): provider-independent protocol, strict schemas rejecting unknown keys/floats/fenced JSON, prompt-injection delimiters, one bounded repair attempt, NVIDIA chat-completions adapter with bounded timeouts/retries.
- **Telegram layer** (`app/telegram/`): update normalization (media/service/edit filtering), Bot API client with error classification, message chunking, and `DeliveryUncertain` for timed-out sends.
- **Services** (`app/services/`): authorization by numeric ID, automatic capture, private clarification lifecycle with round limits, reply-based corrections with append-only revisions, deterministic report facts, Persian template rendering with a numeric guard on AI narrative, on-demand manager reports, and the cron worker with leases, bounded batches and retry backoff.
- **Scripts**: `init_db.py`, `run_worker.py`, `backup_db.py`, `verify_setup.py`, `set_webhook.py`, `report_admin.py`.
- **Tests**: 192 offline pytest tests covering authorization/isolation, webhook security and durability, classification/extraction, clarification, corrections/audit, decimal-safe reporting with currency separation, the 23:30 Asia/Tehran schedule and restart recovery, AI failure modes, migrations, backup/restore, logging safety and project independence.
- **Documentation**: `OPENCODE-HANDOFF.md`, updated `README.md`, `DEPLOYMENT.md`, `CPANEL-DEPLOYMENT.md`, `DECISIONS.md`, `ROADMAP.md`, `TEST-PLAN.md`, `ARCHITECTURE.md`, `DATA-MODEL.md`, `AI-CONTRACT.md`, `SECURITY.md`, `TELEGRAM-SETUP.md`, and `.env.example`.

### Known limitations (documented, intentional for MVP)

- Text input only; media is refused with a short notice in private chats and ignored in groups.
- Persian (Jalali) calendar dates are not produced; report dates are Gregorian ISO.
- No automatic currency-label normalization (e.g. `تومان` vs `IRT` remain separate totals).
- No operator UI; report/delivery state is inspected with `scripts/report_admin.py`.
- A timed-out report send stays `uncertain` until an operator resolves it, by design, to avoid duplicate reports.
