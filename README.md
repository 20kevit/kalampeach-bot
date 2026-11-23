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

## MVP principles

- One Telegram bot, one configured group, one independent database.
- Text messages only in the first release.
- NVIDIA is the AI provider behind a replaceable provider adapter.
- The database and deterministic application code are the source of truth for all figures.
- AI may interpret text and explain results; it must never invent, calculate, or silently alter financial totals.
- Access is controlled by Telegram numeric user IDs and configured chat IDs.

## Build and deployment responsibilities

- **Arena.ai:** writes application code, automated tests, and repository documentation through GitHub only. It does not access cPanel or deploy/run the bot.
- **OpenCode:** later inspects the actual cPanel account, verifies hosting compatibility, and handles deployment, runtime testing, and operation after owner approval.
- **Target runtime:** Python on cPanel shared hosting, using a WSGI/Passenger-compatible HTTPS webhook, durable database inbox, and short-lived cPanel cron worker. Production long polling and always-on background processes are not assumed.

## Documentation map

| File | Purpose |
|---|---|
| [AGENTS.md](AGENTS.md) | Mandatory behavior and workflow for coding agents |
| [PRODUCT-SPEC.md](PRODUCT-SPEC.md) | Functional requirements and user-facing behavior |
| [SCOPE.md](SCOPE.md) | In scope, out of scope, and later |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, boundaries, and processing flows |
| [DATA-MODEL.md](DATA-MODEL.md) | Database entities, event fields, and audit history |
| [AI-CONTRACT.md](AI-CONTRACT.md) | NVIDIA adapter and strict structured-output contract |
| [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md) | BotFather, group visibility, IDs, and private-chat setup |
| [SECURITY.md](SECURITY.md) | Authorization, privacy, secrets, and safe failure |
| [REPORTING.md](REPORTING.md) | Daily and on-demand report rules |
| [TEST-PLAN.md](TEST-PLAN.md) | Acceptance criteria and required tests |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Configuration, startup, operations, and backup expectations |
| [CPANEL-DEPLOYMENT.md](CPANEL-DEPLOYMENT.md) | cPanel shared hosting target and OpenCode deployment handoff |
| [ROADMAP.md](ROADMAP.md) | Delivery stages and future possibilities |
| [CHANGELOG.md](CHANGELOG.md) | User-visible project changes |
| [DECISIONS.md](DECISIONS.md) | Confirmed decisions and unresolved configuration |
| [ARENA-PROMPT.md](ARENA-PROMPT.md) | End-to-end implementation brief for Arena.ai |

## Before running

The deployment must provide Telegram bot credentials, the authorized group ID, manager Telegram user ID, authorized employee IDs, and NVIDIA API configuration. See [DEPLOYMENT.md](DEPLOYMENT.md) and [.env.example](.env.example).

Every employee must open the bot and press **Start** once before the bot can send them private clarification messages. The bot must be configured to receive ordinary group messages; see [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md).

## Repository layout

```
passenger_wsgi.py              Passenger WSGI entry point (exposes `application`)
kalampeach/
  config.py                    Environment/.env loading + strict validation
  db.py                        SQLite wrapper (WAL, FK on, short-lived connections)
  migrations/                   Versioned SQL migrations + safe runner
  repository.py               All SQL (parameterized only)
  timeutil.py, decimal_utils.py, logging_utils.py
  ai/                          Provider contract, JSON schema validation, NVIDIA adapter
  telegram/                    Update parsing, Bot API client, chunking
  services/                    auth, capture, corrections, clarification, reporting,
                               reports (daily + on-demand), processor, worker
  web/app.py                   Dependency-free WSGI webhook + /health
scripts/
  run_worker.py                Cron: one bounded, idempotent worker run
  migrate.py                   --status / --dry-run / --apply
  doctor.py                    Read-only configuration + storage check
  backup_db.py                 Online SQLite backup with integrity check
  telegram_webhook.py          setWebhook / getWebhookInfo / deleteWebhook (never run here)
tests/                         Offline pytest suite (fake AI, fake Telegram, temp DB)
```

## Running locally (no deployment)

```bash
python3 -m pip install -r requirements-dev.txt    # pytest only
python3 -m pytest                                  # offline suite
```

The suite never contacts Telegram, NVIDIA, cPanel or the accounting website. It uses a
temporary SQLite database, a scripted AI provider, a recording Telegram client and a
controllable clock.

To exercise the operational scripts against a throwaway database (requires a private
environment file or exported variables such as those in `.env.example`):

```bash
export DATABASE_PATH=/tmp/kalampeach-demo/private/kalampeach_bot.sqlite3
python3 scripts/migrate.py --status     # 001 initial_schema   PENDING
python3 scripts/migrate.py --apply      # applied 001 initial_schema
python3 scripts/doctor.py               # read-only PASS/FAIL report
python3 scripts/run_worker.py           # one bounded cron-equivalent run
python3 scripts/backup_db.py --output /tmp/kalampeach-demo/backups
```

`/health` returns `200` only when the database is reachable **and** the shipped schema
version is applied; otherwise it returns `503` with `status: degraded`, so a web
request never serves traffic against an unmigrated database.

## Required configuration

All keys are required unless marked optional in [.env.example](.env.example). Values
that only the owner can supply are listed in [DECISIONS.md](DECISIONS.md).

| Key | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token (secret) |
| `TELEGRAM_GROUP_ID`, `MANAGER_TELEGRAM_ID`, `EMPLOYEE_TELEGRAM_IDS` | Numeric-ID authorization |
| `TELEGRAM_WEBHOOK_SECRET`, `WEBHOOK_PUBLIC_URL` | Webhook authentication and route |
| `NVIDIA_API_KEY`, `NVIDIA_BASE_URL`, `NVIDIA_MODEL` | AI provider (no defaults are invented) |
| `BUSINESS_TIMEZONE` (`Asia/Tehran`), `DAILY_REPORT_TIME` (`23:30`) | Business calendar |
| `DEFAULT_CURRENCY` (optional, empty by default) | Only if the owner wants a default currency |
| `DATABASE_PATH` | Absolute path outside `public_html` |
| `LOG_LEVEL`, optional `ENV_FILE`, worker limits | Operations |

## Project status

- Application code, migrations, operational scripts, tests and this documentation are
  complete for the MVP scope in `SCOPE.md`.
- Automated tests exist for every automated section of [TEST-PLAN.md](TEST-PLAN.md)
  (A–G). Section H (human acceptance with real Telegram accounts) has **not** been
  performed, because Arena must not deploy, register a webhook, configure cron or message
  real users.
- Nothing has been deployed, migrated on a real host, or exercised against live Telegram
  or NVIDIA endpoints. Exact locally executed commands and results are recorded in
  [CHANGELOG.md](CHANGELOG.md) and must be reproduced by OpenCode on the account.
- OpenCode's remaining work: cPanel inspection, dependency/runtime verification,
  configuration, migration, webhook registration, cron setup, and the human acceptance
  test. See [CPANEL-DEPLOYMENT.md](CPANEL-DEPLOYMENT.md).
