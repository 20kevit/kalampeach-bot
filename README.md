# KalamPeach Telegram Operations Bot

An independent Telegram bot for capturing Kalam Pich business activity from one staff group and authorized private chats, storing validated events in its own database, and producing manager reports with NVIDIA AI assistance.

> **Project boundary:** This repository is a new, standalone system. It must not connect to, import from, or reuse the code, database, API, credentials, or infrastructure of the separate KalamPeach accounting web application.

## What it does

- Reads messages in one configured Telegram group and identifies business-relevant information.
- Lets authorized employees record activity in private chat using natural language.
- Captures sales/revenue, expenses/purchases, inventory changes/consumption, and daily incidents/notes.
- Saves only validated structured events; ignores ordinary conversation.
- Asks the original employee privately when required information is genuinely ambiguous.
- Allows an employee to correct a previous event by replying to the original Telegram message.
- Preserves an audit trail of every correction.
- Sends a daily report to the manager at **23:30 Asia/Tehran**.
- Lets the manager request reports in natural language at any time.

## Implementation at a glance

| Topic | Choice |
|---|---|
| Language | Python **3.11** target, minimum **3.9** |
| Web framework | **Flask 3.0.3** (WSGI), served by cPanel Passenger through `passenger_wsgi.py` |
| Telegram transport | HTTPS webhook with secret-token validation; **no long polling** |
| Queue | SQLite durable inbox (`telegram_inbox`), drained by a cron worker with database leases |
| Background work | `scripts/run_worker.py`, invoked by cPanel cron every minute; short-lived, no daemon |
| Database | SQLite (stdlib `sqlite3`), versioned SQL migrations, file outside the web root |
| AI | NVIDIA chat-completions adapter behind `app.ai.provider.AIProvider`; endpoint/model/key are configuration |
| Money | Decimal strings + `Decimal` arithmetic; currencies never merged |
| Tests | 192 pytest tests, offline (fake AI/Telegram, temporary database) |

## Repository layout

```text
passenger_wsgi.py            Passenger entry point exposing `application`
app/
  wsgi.py                    Flask app: webhook secret check, durable enqueue, /healthz
  config.py                  Strict environment/env-file validation (fail closed)
  bootstrap.py               Builds the application context (AI, Telegram, DB, clock)
  db.py                      SQLite connection/pragmas, transactions, migration runner
  migrations/                Append-only SQL migrations (0001_initial.sql)
  domain/events.py           Event categories, required fields, snapshots, semantic rules
  money.py                   Decimal-safe money/quantity parsing and formatting
  timeutil.py                UTC storage helpers + Asia/Tehran business dates
  messages_fa.py             Every Persian user-facing string
  ai/                        Provider protocol, strict schemas, prompts, NVIDIA adapter
  telegram/                  Update normalization and Bot API client
  repo/                      SQL persistence per aggregate (inbox, messages, events, …)
  services/                  Authorization, capture, clarification, correction,
                             reports (facts/render/delivery), processor, worker
scripts/
  run_worker.py              Cron worker (process inbox + due reports)
  init_db.py                 Create data directory (0700) and apply migrations
  backup_db.py               Verified SQLite online backup
  verify_setup.py            Read-only diagnostics for the deployment phase
  set_webhook.py             Webhook inspection/registration (requires --confirm)
  report_admin.py            Inspect deliveries, resolve `uncertain` sends
tests/                       192 offline tests mapped to TEST-PLAN.md
```

## Quick start (local development only)

Local runs use fakes in tests and never contact Telegram or NVIDIA.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Run the whole suite (offline, no credentials needed):
.venv/bin/python -m pytest -q

# Optional: create a local database from a private env file
cp .env.example .env.local        # fill placeholders locally, never commit
.venv/bin/python scripts/init_db.py --env-file .env.local
.venv/bin/python scripts/verify_setup.py --env-file .env.local
```

## Build and deployment responsibilities

- **Arena.ai:** wrote application code, automated tests, and repository documentation through GitHub only. It did not access cPanel, deploy, register a webhook, create cron jobs, or message real users.
- **OpenCode:** later inspects the actual cPanel account, verifies hosting compatibility, and handles deployment, runtime testing, and operation after owner approval. See [OPENCODE-HANDOFF.md](OPENCODE-HANDOFF.md) and [CPANEL-DEPLOYMENT.md](CPANEL-DEPLOYMENT.md).
- **Target runtime:** Python on cPanel shared hosting, using a WSGI/Passenger-compatible HTTPS webhook, durable database inbox, and short-lived cPanel cron worker.

## Documentation map

| File | Purpose |
|---|---|
| [AGENTS.md](AGENTS.md) | Mandatory behavior and workflow for coding agents |
| [PRODUCT-SPEC.md](PRODUCT-SPEC.md) | Functional requirements and user-facing behavior |
| [SCOPE.md](SCOPE.md) | In scope, out of scope, and later |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, boundaries, processing flows, module map |
| [DATA-MODEL.md](DATA-MODEL.md) | Database entities, event fields, and audit history |
| [AI-CONTRACT.md](AI-CONTRACT.md) | NVIDIA adapter and strict structured-output contract |
| [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md) | BotFather, group visibility, IDs, and private-chat setup |
| [SECURITY.md](SECURITY.md) | Authorization, privacy, secrets, and safe failure |
| [REPORTING.md](REPORTING.md) | Daily and on-demand report rules |
| [TEST-PLAN.md](TEST-PLAN.md) | Acceptance criteria, test mapping, local results |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Configuration, migrations, cron, logs, backup/restore |
| [CPANEL-DEPLOYMENT.md](CPANEL-DEPLOYMENT.md) | cPanel shared hosting target and deployment shape |
| [OPENCODE-HANDOFF.md](OPENCODE-HANDOFF.md) | Exact later inspection/setup/test/webhook/cron steps |
| [ROADMAP.md](ROADMAP.md) | Delivery stages and future possibilities |
| [CHANGELOG.md](CHANGELOG.md) | User-visible project changes |
| [DECISIONS.md](DECISIONS.md) | Confirmed decisions, implementation choices, open inputs |
| [ARENA-PROMPT.md](ARENA-PROMPT.md) | End-to-end implementation brief for Arena.ai |

## Before running in production

The deployment must supply Telegram bot credentials, the authorized group ID, manager Telegram user ID, authorized employee IDs, webhook secret/URL, and NVIDIA API key/base URL/model. See [DEPLOYMENT.md](DEPLOYMENT.md) and [.env.example](.env.example).

Every employee must open the bot and press **Start** once before the bot can send them private clarification messages. The bot must be configured to receive ordinary group messages; see [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md).

## Project status

- **Repository implementation:** complete for the MVP scope (code, migrations, tests, docs).
- **Automated tests:** authored and executed locally by Arena in an offline environment (192 passed, Python 3.11.2, Linux). See [TEST-PLAN.md](TEST-PLAN.md) for the mapping and evidence.
- **cPanel deployment, live webhook, cron jobs, and real-user messaging:** **not performed** and not verified. Those remain OpenCode/owner work after approval.
