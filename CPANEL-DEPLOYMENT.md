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

The exact route and Passenger startup file may differ if cPanel's actual Python application setup requires it; document any deviation in `DECISIONS.md`.

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

## 9. Explicit non-goals

Do not require:
- VPS/root access.
- systemd or supervisor.
- Docker or Kubernetes.
- Redis, Celery, or an external message queue.
- A permanently running Python process.
- Long polling.
- A separate database server unless the actual cPanel environment makes SQLite unsuitable and the owner approves the alternative.
