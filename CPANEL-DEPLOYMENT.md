# cPanel Shared Hosting Deployment Target

This guide defines the intended deployment shape and the concrete steps for the
later operations phase. It is a handoff document for OpenCode; it is **not**
evidence that any specific cPanel plan supports every requirement, and Arena has
not executed any of it.

For the step-by-step checklist and the exact owner inputs still needed, see
[OPENCODE-HANDOFF.md](OPENCODE-HANDOFF.md).

## 1. Target constraints

The application must run on ordinary Python-enabled cPanel shared hosting with the
features actually available on the account.

Expected facilities:
- cPanel Python application setup (often labelled **Setup Python App**) with
  Passenger/WSGI support.
- A public HTTPS URL routed to the Python application.
- cPanel Cron Jobs, preferably allowing a one-minute interval.
- Outbound HTTPS from Python to Telegram Bot API and the configured NVIDIA API.
- Writable durable storage under the hosting account home directory.
- Python's SQLite module and a reliable local filesystem if SQLite is selected.

OpenCode must verify each item on the real hosting plan before deployment. Do not
assume availability based on the phrase "Python hosting".

## 2. Application runtime model (as implemented)

### Web requests

- Entry point: **`passenger_wsgi.py`** in the repository root, exposing a
  module-level `application` (a Flask WSGI app). Passenger application root = the
  repository directory.
- Routes: `POST /telegram/webhook` and `GET /healthz` (no secrets in responses).
- The webhook requires `X-Telegram-Bot-Api-Secret-Token` and compares it in constant
  time with `TELEGRAM_WEBHOOK_SECRET`. Invalid requests get 403 and are never stored.
- Valid updates are written to `telegram_inbox` keyed by unique `update_id` in one
  short transaction, and only then acknowledged. Duplicates are acknowledged without
  a second row.
- No AI, report generation, or long processing happens inside the request. If the
  schema is not initialised the endpoint returns 503, so a bad deploy is visible.
- If configuration is invalid, the WSGI entry point serves a 503
  `configuration_incomplete` response and logs the affected key **names**.

### Background processing

Shared hosting must not be assumed to keep arbitrary Python processes alive, so:
- No Telegram long polling; no in-process timers, threads, APScheduler, Celery or
  permanent daemon.
- **`scripts/run_worker.py`** is a short-lived command that claims a bounded batch
  (default 20 rows, default runtime budget 50 s), processes each update, releases any
  unsupervised claims, evaluates due daily reports (max 2 per run), prints a JSON
  summary and exits.
- Claims are **database leases** (`claim_token`, `claimed_until_utc`) with an
  `attempts` counter, so overlapping cron invocations cannot process one update twice
  and a crash loop cannot retry forever.
- Retryable failures (AI/Telegram/network) use bounded exponential backoff; the row
  becomes `failed` with a stable error category after `INBOX_MAX_ATTEMPTS`.

## 3. cPanel Cron Jobs

Preferred setup: invoke the worker once per minute. Application code, not the server's
cron timezone, decides whether the daily report is due.

Replace paths with the actual cPanel account paths and the interpreter shown by
cPanel after creating the Python application:

```cron
* * * * * /home/CPANEL_USER/virtualenv/APP_ROOT/3.11/bin/python /home/CPANEL_USER/APP_ROOT/scripts/run_worker.py --env-file /home/CPANEL_USER/private/kalampeach.env >> /home/CPANEL_USER/logs/kalampeach-worker.log 2>&1
```

Worker requirements and how they are met:

| Requirement | Implementation |
|---|---|
| Bounded runtime/batch, exit cleanly | `WORKER_MAX_RUNTIME_SECONDS`, `WORKER_BATCH_SIZE`, release of unprocessed claims, `run_worker.py` returns/exits |
| Asia/Tehran due calculation | `app/timeutil.py` + `app/services/reporting.py` using `zoneinfo` |
| Recover a missed 23:30 tick | `REPORT_CATCHUP_DAYS` (default 1) + persisted delivery rows |
| No duplicate reports | `report_deliveries.report_key` UNIQUE + delivery lease |
| Overlap safety | inbox leases, `BEGIN IMMEDIATE` transactions, unique keys |
| Concise logs without content | JSON logs with categories and IDs only |

If the provider does not allow one-minute cron, OpenCode must report the limitation
and estimate processing/report delay before proposing an alternative. Do not silently
switch to an always-on process.

## 4. Database and filesystem

SQLite is used, with a documented fallback below if the account proves unsuitable.

- Database file: `/home/CPANEL_USER/data/kalampeach_bot.sqlite3` (0700 directory,
  0600 file, outside `public_html`). `DATABASE_PATH` rejects public web roots.
- `PRAGMA foreign_keys=ON`, `busy_timeout=5000`; WAL is requested and tolerated if the
  filesystem refuses it (correctness does not depend on WAL).
- Short transactions; the webhook never holds a transaction while calling the network.
- Migrations: `python scripts/init_db.py --env-file …` (idempotent, additive, never
  destructive).
- Backups: `python scripts/backup_db.py --dest /home/CPANEL_USER/backups --env-file …`
  (online backup API, 0600, integrity check, row counts). Restore procedure is in
  `DEPLOYMENT.md` §8.
- Logs: stderr by default (cPanel error log + cron redirect), optional `LOG_FILE`
  outside `public_html`.

**SQLite fallback:** if the real filesystem cannot lock reliably (for example a
network filesystem with broken POSIX locking), stop and report the evidence with the
observed error. The smallest supported alternative (for example a MySQL database made
available by that cPanel plan) requires owner approval and a new migration set; do not
introduce a hosted queue or a new external service.

## 5. Secrets and environment

Configure secrets through cPanel's supported environment mechanism **and/or** a private
env file with restrictive permissions. The application prefers existing process
variables and falls back to `KALAMPEACH_ENV_FILE` / `ENV_FILE` for cron, which often
does not inherit the Passenger environment.

Required values include:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_GROUP_ID`
- `MANAGER_TELEGRAM_ID`
- `EMPLOYEE_TELEGRAM_IDS`
- `TELEGRAM_WEBHOOK_SECRET`
- `NVIDIA_API_KEY`
- `NVIDIA_BASE_URL`
- `NVIDIA_MODEL`
- `WEBHOOK_PUBLIC_URL`
- `DATABASE_PATH`
- `BUSINESS_TIMEZONE=Asia/Tehran`
- `DAILY_REPORT_TIME=23:30`

Generate the webhook secret with, for example,
`python -c "import secrets; print(secrets.token_urlsafe(48))"` and store it only in
the private environment. Never commit real values. Verify that Passenger **and** cron
both see the same configuration (`scripts/verify_setup.py` prints a secret-free view).

## 6. Telegram webhook setup

After the application is deployed to a public HTTPS URL and the owner approves:

```bash
# read-only inspection
python scripts/set_webhook.py --info --env-file /home/CPANEL_USER/private/kalampeach.env

# registration (LIVE CHANGE — only after owner approval)
python scripts/set_webhook.py --confirm --env-file /home/CPANEL_USER/private/kalampeach.env
```

Then verify:
1. `getWebhookInfo` shows the correct URL, no `last_error_message`, and
   `allowed_updates = ["message"]`.
2. A test update appears as a row in `telegram_inbox` and is acknowledged.
3. A duplicate delivery does not create a second inbox row.
4. The cron worker drains the row and marks it `done`/`ignored`.

Webhook registration is a live external change. Arena did not perform it.

## 7. cPanel-specific OpenCode inspection checklist

Before changing anything, OpenCode must report read-only evidence for:
- available Python versions in cPanel, and the virtualenv path;
- presence and version of Setup Python App / Passenger, and the WSGI entry-point
  requirement (does it accept `passenger_wsgi.py` with `application`?);
- cron minimum interval and execution limits (one-minute allowed?);
- package installation method (`pip install -r requirements.txt` inside the venv);
- inbound HTTPS route availability and a valid TLS certificate;
- outbound HTTPS to `api.telegram.org` and the configured NVIDIA base URL;
- SQLite availability/version, filesystem type, and write permissions (including
  whether WAL is accepted and locking behaves under two processes);
- account disk quota, memory/process limits, CPU limits, request timeouts;
- how environment variables are exposed to Passenger and to cron;
- whether the application restarts automatically after file changes or needs the
  cPanel "Restart" action.

Do not assume root access, shell services, systemd, Docker, or persistent background
processes.

## 8. OpenCode deployment sequence

Only after owner approval:

1. Back up existing data; for a first deployment confirm the target paths are empty.
2. Confirm host capabilities against this document and record the evidence.
3. Create the cPanel Python application (application root = repository) and virtualenv.
4. Install pinned requirements: `pip install -r requirements.txt`.
5. Create `data/`, `private/`, `backups/`, `logs/` with 0700; write the private env file
   with 0600.
6. `python scripts/init_db.py --env-file …` (non-destructive; verify version).
7. `python scripts/verify_setup.py --env-file …` (must report no problems).
8. Restart the Passenger application and probe `GET /healthz` over HTTPS.
9. Configure the cron worker (every minute) and confirm a run exits 0 with a JSON
   summary.
10. Register the Telegram webhook with the secret token (after approval).
11. Run controlled tests with approved test accounts (see `TEST-PLAN.md` §H).
12. Prove the daily report due-job with a controlled clock/fixture — do not wait until
    23:30 in production. Suggested: a temporary test database with a seeded event and
    `REPORT_CATCHUP_DAYS=0`, then a manual `run_worker.py` invocation after 23:30
    Tehran, or an approved one-off fixture.
13. Verify backup and restore with `backup_db.py` and the documented restore drill.
14. Deliver the operational handover: commands, paths, log locations, limitations,
    recovery steps, and the results of every test actually executed.

## 9. Explicit non-goals

Do not require:
- VPS/root access.
- systemd or supervisor.
- Docker or Kubernetes.
- Redis, Celery, or an external message queue.
- A permanently running Python process.
- Long polling.
- A separate database server unless the actual cPanel environment makes SQLite
  unsuitable and the owner approves the alternative.

## 10. What Arena did and did not do

- **Did:** write the application, migrations, tests, scripts and this documentation;
  run the automated test suite locally in an offline environment.
- **Did not:** access cPanel/SSH, create a Python application, install packages on the
  host, create the database on the host, register a webhook, create cron jobs, send
  any Telegram message, call NVIDIA, or touch the separate accounting system.
- No hosting or runtime claim in this repository has been verified on real hardware.
