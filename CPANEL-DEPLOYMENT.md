# cPanel Shared Hosting Deployment Target

This guide defines the intended deployment shape. It is a handoff document for OpenCode; it is not evidence that any specific cPanel plan supports every requirement.

## 1. Target constraints

The application must run on ordinary Python-enabled cPanel shared hosting with the features actually available on the account.

Expected facilities:
- cPanel Python application setup (often labelled **Setup Python App**) with Passenger/WSGI support.
- A public HTTPS URL routed to the Python application.
- cPanel Cron Jobs, preferably allowing a one-minute interval.
- Outbound HTTPS from Python to Telegram Bot API and the configured NVIDIA API.
- Writable durable storage under the hosting account home directory.
- Python's SQLite module and a reliable local filesystem if SQLite is selected.

OpenCode must verify each item on the real hosting plan before deployment. Do not assume availability based on the phrase “Python hosting”.

## 2. Application runtime model

### Web requests

- Provide a WSGI-compatible application entry point, normally `passenger_wsgi.py`.
- Configure the cPanel Python application to point to the repository/application directory and the selected Python version.
- Expose a small webhook route, for example `/telegram/webhook`.
- Require HTTPS.
- Validate the `X-Telegram-Bot-Api-Secret-Token` header against `TELEGRAM_WEBHOOK_SECRET`.
- Reject invalid requests without processing them.
- Persist valid Telegram updates into a durable database inbox, keyed uniquely by Telegram `update_id`.
- Return success promptly only after the update is durably stored.
- Do not call NVIDIA or perform lengthy business processing inside the webhook request.

The implemented entry point is `passenger_wsgi.py`, which exposes `application` from
`kalampeach/web/app.py`. The webhook path comes from `WEBHOOK_PUBLIC_URL` and must match the
route Passenger forwards (default `/telegram/webhook`). `GET|HEAD /health` reports
`schema_version` and `expected_schema_version`; it returns **503 `degraded`** until the
shipped schema version is applied, so a premature public URL cannot silently serve an
unmigrated database.

The exact route and Passenger startup file may differ if cPanel's actual Python application
setup requires it; document any deviation in `DECISIONS.md`.

### Background processing

Shared hosting must not be assumed to keep arbitrary Python processes alive. Therefore:
- Do not use Telegram long polling as the production transport.
- Do not rely on in-process timers, background threads, APScheduler, Celery workers, or a permanent daemon.
- Provide a short-lived command such as `scripts/run_worker.py`.
- The worker claims a bounded batch of queued updates, processes them, handles retry state, evaluates due reports, and exits.
- The worker must be safe when two cron invocations overlap.
- Use database leases/locks with expiry and idempotency constraints; do not rely only on process-local locks.

## 3. cPanel Cron Jobs

Preferred setup: invoke the worker once per minute. Application code, not the server's cron timezone, decides whether the daily report is due.

Illustrative command only — OpenCode must replace paths with the actual cPanel account paths:

```cron
* * * * * /home/CPANEL_USER/virtualenv/APP_ROOT/PYTHON_VERSION/bin/python /home/CPANEL_USER/APP_ROOT/scripts/run_worker.py >> /home/CPANEL_USER/logs/kalampeach-worker.log 2>&1
```

The exact virtualenv path is shown by cPanel after creating the Python application. Do not copy the placeholder command literally.

Worker requirements:
- Have a bounded maximum runtime/batch size suitable for shared-hosting limits.
- Exit cleanly after each run.
- Use Asia/Tehran via an IANA timezone database to decide whether the 23:30 daily report is due.
- Recover a report missed because cron did not run exactly at 23:30.
- Use a unique report key and persisted delivery state to avoid duplicate reports.
- Avoid overlapping work through database-safe claim/lease logic.
- Log concise operational status without message bodies, API keys, or bot tokens.

If the hosting provider does not allow one-minute cron, OpenCode must report the limitation and estimate the resulting processing/report delay before proposing an alternative. Do not silently switch to an always-on process.

Worker CLI (all flags optional; defaults come from the environment):

```bash
python scripts/run_worker.py [--batch-size N] [--max-seconds S] [--skip-reports]
```

It prints one JSON summary line (for example
`{"claimed": 0, "done": 0, "retried": 0, "failed": 0, "duplicates": 0, "lease_lost": 0,
"phase_failures": {}, "clarifications": {...}, "reports": {...}, "schema_version": 2}`)
and exits with status `0`; it exits `1` when a phase failed during that run (the summary
is still printed, and `phase_failures` names the phase). `--skip-reports` is useful while
testing intake only. The worker also applies pending migrations at startup, so cron alone
can bring a fresh installation up to date and `/health` becomes `200` once that has
happened.

**Cron applies pending migrations.** That is convenient on a fresh install but it means a
schema upgrade can run unattended. Before upgrading an existing installation, run
`migrate.py --apply` deliberately (see the upgrade procedure in section 4) rather than
letting the next cron tick do it.

## 4. Database and filesystem

SQLite is the preferred MVP database only if the actual host supports it reliably.

- Store the database under a private path such as `/home/CPANEL_USER/data/kalampeach_bot.sqlite3`.
- Never put the database, environment file, logs, backups, or secrets under `public_html`.
- Ensure the Python application user can read/write the database directory.
- Use short transactions and avoid simultaneous long writes from webhook and cron worker.
- Use an appropriate SQLite journal mode supported by the host; verify locking behavior on the actual filesystem.
- Include migrations and a tested backup/restore procedure.
- Keep backups outside the web root and restrict file permissions.

If SQLite locking or filesystem semantics are unsuitable on the actual account, stop and report the evidence. Propose the smallest supported relational database option available in that cPanel plan; do not introduce a new hosted service without owner approval.

Implemented commands (paths are placeholders for the real account):

```bash
python scripts/migrate.py --status     # 001 initial_schema PENDING|applied, 002 … idem
python scripts/migrate.py --dry-run    # show what would run
python scripts/migrate.py --apply      # apply pending migrations (never drops data)
python scripts/doctor.py [--env-file /home/CPANEL_USER/kalampeach/.env]
python scripts/backup_db.py --output /home/CPANEL_USER/backups --keep 14
```

`migrate.py` refuses to continue if an already-applied migration file changed its checksum.
`doctor.py` is read-only (no webhook, cron, Telegram or NVIDIA call) and masks secret values.
`backup_db.py` writes a verified copy with mode `0600`, refuses any output path inside
`public_html`, and never overwrites the live database. Restore procedure:

1. Disable the cron job.
2. Copy the chosen `kalampeach_bot_<timestamp>.sqlite3` over `DATABASE_PATH`
   (keep the replaced file until verification succeeds).
3. Run `python scripts/migrate.py --status` and `python scripts/doctor.py`.
4. Re-enable cron and confirm `/health` returns `200`.

Verified in this repository against a throwaway database: `--status` →
`001 initial_schema PENDING`, `002 clarification_delivery_states PENDING`; `--apply` →
`applied 001 …` then `applied 002 …`; `doctor.py` → `Result: 0 failure(s), 0 warning(s)`
including `[PASS] delivery_review_needed — none`; `backup_db.py` →
`backup_created … integrity=ok` with `-rw-------`. None of this has been run on a cPanel
host.

### Upgrading an existing 0.1.0 database

Schema version 2 rebuilds the `clarifications` table (SQLite cannot alter a CHECK
constraint in place). It preserves ids, timestamps, foreign keys and the id sequence, and
runs inside the same single transaction as every other migration — a failure leaves the
database untouched. Because it rewrites a table:

1. Take a backup first: `python scripts/backup_db.py --output …` and confirm
   `integrity=ok`.
2. Pause the cron entry for the duration of the upgrade (webhook traffic can continue;
   it only inserts inbox rows).
3. `python scripts/migrate.py --status` → expect `002 … PENDING`.
4. `python scripts/migrate.py --apply`.
5. `python scripts/migrate.py --status` → both `applied`; `python scripts/doctor.py` →
   `0 failure(s)` and `database_open — quick_check=ok`.
6. Re-enable cron. `/health` reports `200` only with schema version 2 applied.

There is no downgrade path. Restoring the pre-upgrade backup is the documented rollback.

## 5. Secrets and environment

Configure secrets through cPanel's supported environment-variable mechanism or a private configuration file outside the web root with restrictive permissions.

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

Do not commit real values. Do not print secrets in diagnostics. Verify whether cPanel exposes environment variables to both Passenger web requests and cron processes; cron may need an explicit secure environment-loading mechanism.

## 6. Telegram webhook setup

After the application is deployed to a public HTTPS URL and the owner approves:
1. Verify the webhook route is reachable over HTTPS.
2. Configure Telegram's webhook with the exact URL and secret token.
3. Restrict allowed update types to the message types needed by the MVP.
4. Verify a test update is durably stored and acknowledged.
5. Verify duplicate delivery does not create duplicate inbox rows.
6. Verify webhook status and error logs.

Webhook registration is a live external change. Arena must not perform it. OpenCode must wait for owner approval.

The repository ships `scripts/telegram_webhook.py` for this, which requires an explicit
confirmation flag because it changes live state:

```bash
python scripts/telegram_webhook.py info
python scripts/telegram_webhook.py set --yes      # sets WEBHOOK_PUBLIC_URL + secret
python scripts/telegram_webhook.py delete --yes
```

`info` only calls `getWebhookInfo`. `set` sends `allowed_updates=["message"]` and the
configured secret token. This script has **not** been executed from this repository.

### Delivery review after go-live

Two read-only signals tell an operator that a delivery outcome is unknown:

- `GET /health` → `review_needed` with `clarifications_uncertain`, `reports_uncertain`
  and `total` (counts only).
- `python scripts/doctor.py` → `delivery_review_needed` (`PASS` when zero, `WARN`
  otherwise).

`WARN`/non-zero is not an error to clear by editing the database. Investigate the
matching log events (`clarification_delivery_uncertain`, `report_delivery_uncertain`,
`report_delivery_recovered`) and let the employee/manager re-send the information or
answer by reply. The code deliberately has no automatic resend for unknown outcomes.

`scripts/run_worker.py` exits `0` on a clean run and `1` when a phase failed. A `1` with
`"phase_failures": {"clarifications": "..."}` in the printed JSON means that phase
raised; the other phases (including the daily report) still completed.

## 7. cPanel-specific OpenCode inspection checklist

Before changing anything, OpenCode must report read-only evidence for:
- cPanel account's available Python versions.
- Presence and version of Setup Python App / Passenger.
- WSGI entry-point configuration requirements.
- Cron minimum interval and execution limits.
- Python virtualenv path and package installation method.
- Inbound HTTPS route availability and TLS certificate.
- Outbound HTTPS access to Telegram and NVIDIA.
- SQLite availability, filesystem type/behavior, and write permissions.
- Account disk quota, memory/process limits, CPU limits, and request timeouts.
- How environment variables are exposed to Passenger and cron.
- Whether the application is restarted automatically after file changes or needs a cPanel restart action.

Do not assume root access, shell services, systemd, Docker, or persistent background processes.

## 8. OpenCode deployment sequence

Only after owner approval:
1. Back up any existing application data (for a first deployment, confirm the target path is empty).
2. Confirm actual host capabilities against this document.
3. Create the cPanel Python application and virtual environment.
4. Install pinned requirements.
5. Configure private environment variables and data/log directories.
6. Initialize/migrate the database without destructive reset.
7. Verify the WSGI app and webhook endpoint with safe test requests.
8. Configure cPanel cron worker.
9. Register the Telegram webhook using the secret token.
10. Run controlled tests with approved test accounts.
11. Verify the daily report due-job behavior using a test clock or controlled due-job fixture; do not wait until 23:30 in production to prove scheduling.
12. Confirm the real 23:30 Asia/Tehran report only after owner approval.
13. Verify backup and restore.
14. Provide an operational handover with commands, paths, logs, limitations, and recovery steps.

## 9. Verification status: repository vs host

| Check | Repository (executed) | cPanel host (OpenCode) |
|---|---|---|
| Offline test suite | `python3 -m pytest` → 295 passed | must reproduce on the account's Python |
| Passenger entry point | smoke-tested via `wsgiref` with a temp DB (health, signed update) | create the Python app and confirm the real URL |
| Migrations | `--status/--apply/--status` executed on a temp DB; 001→002 upgrade and concurrent/checksum cases covered by tests | run against `DATABASE_PATH` after backup |
| Doctor | `0 failure(s), 0 warning(s)` with a private env file | run with the account's real values |
| Worker | empty-queue run exits `0` with JSON summary (`schema_version: 2`, `phase_failures: {}`) | configure cron, confirm one-minute interval |
| Backup | verified copy + restore rehearsal on temp files | confirm destination, retention, permissions |
| Webhook registration | **not executed** | owner-approved `set --yes` |
| Real Telegram/NVIDIA traffic | **never** | controlled test accounts after approval |
| 23:30 report in production | simulated in tests only | observe one real evening after approval |
| Delivery-review surface (`/health` `review_needed`, doctor `delivery_review_needed`) | executed locally against a temp DB | read both on the account after the first real week |

## 10. Explicit non-goals

Do not require:
- VPS/root access.
- systemd or supervisor.
- Docker or Kubernetes.
- Redis, Celery, or an external message queue.
- A permanently running Python process.
- Long polling.
- A separate database server unless the actual cPanel environment makes SQLite unsuitable and the owner approves the alternative.
