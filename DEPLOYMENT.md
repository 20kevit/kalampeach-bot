# Deployment and Configuration

The target deployment is a Python application on shared hosting managed through cPanel. Arena.ai prepares the code and documentation only; it has not deployed. OpenCode will later inspect and deploy it after the owner approves the operational steps.

## 1. Runtime version and dependencies

- Python **3.11** target, minimum **3.9** (`zoneinfo`).
- Pinned runtime dependencies (`requirements.txt`): `Flask==3.0.3`, `requests==2.32.3`, `tzdata==2024.2`.
- Development only (`requirements-dev.txt`): `pytest==8.3.3`.
- All pure-Python wheels; no compiler needed in the cPanel virtual environment.
- Do **not** install the development requirements on production.

## 2. Required configuration

All values come from process environment and/or a private env file
(`KALAMPEACH_ENV_FILE` or `ENV_FILE`). Existing process variables win over the file,
so the cPanel application environment and a cron-friendly private file can coexist.
`.env.example` documents every key; no real value is committed.

| Key | Required | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | BotFather token (secret) |
| `TELEGRAM_GROUP_ID` | yes | The one configured group (usually negative) |
| `MANAGER_TELEGRAM_ID` | yes | Numeric manager user ID |
| `EMPLOYEE_TELEGRAM_IDS` | yes* | Comma-separated numeric employee IDs |
| `TELEGRAM_WEBHOOK_SECRET` | yes | 16–256 chars of `A-Za-z0-9_-`; must match `setWebhook` |
| `WEBHOOK_PUBLIC_URL` | yes | Public HTTPS webhook URL |
| `NVIDIA_API_KEY` | yes | Secret |
| `NVIDIA_BASE_URL` | yes | HTTPS base of the chat-completions API |
| `NVIDIA_MODEL` | yes | Exact model ID available to the account |
| `DATABASE_PATH` | yes | Absolute SQLite path outside any web root |
| `BUSINESS_TIMEZONE` | yes | Must be `Asia/Tehran` (validated) |
| `DAILY_REPORT_TIME` | yes | Must be `23:30` (validated) |
| `DEFAULT_CURRENCY` | no | Applied only when a message omits currency; empty = ask |
| `TELEGRAM_API_BASE_URL` | no | Default `https://api.telegram.org` |
| `MANAGER_CAN_RECORD_EVENTS` | no | Default `false` |
| `SEND_GROUP_ACKNOWLEDGEMENTS` | no | Default `true` |
| `ALLOW_MESSAGE_DATE_AS_EVENT_DATE` | no | Default `false` (ask instead of assuming) |
| `REPORT_NARRATIVE_ENABLED` | no | Default `true`; template fallback always available |
| `REPORT_CATCHUP_DAYS` | no | Default `1` (0–7) |
| `NVIDIA_TIMEOUT_SECONDS` / `NVIDIA_MAX_RETRIES` / `NVIDIA_TEMPERATURE` / `NVIDIA_JSON_MODE` | no | Defaults 30 / 2 / 0 / false |
| `LOG_FILE` / `LOG_LEVEL` | no | Empty file = stderr (cPanel error log); default `INFO` |
| `WORKER_MAX_RUNTIME_SECONDS` / `WORKER_BATCH_SIZE` | no | Defaults 50 / 20 |
| `INBOX_MAX_ATTEMPTS` / `REPORT_MAX_ATTEMPTS` | no | Defaults 5 / 5 |
| `MAX_CLARIFICATION_ROUNDS` / `MAX_REPORT_RANGE_DAYS` | no | Defaults 3 / 31 |

\* The configuration is invalid if no identity can record (no employees and manager
recording disabled), because the bot could never capture anything.

Startup fails closed: a missing key produces a log naming the key and a 503 webhook
response, with no partial operation and no secret in the message.

## 3. Application layout on the host

```text
/home/CPANEL_USER/
├── APP_ROOT/                 # this repository (Passenger application root)
├── data/kalampeach_bot.sqlite3   # 0700 dir, 0600 file, outside public_html
├── private/kalampeach.env    # 0600 env file for cron and Passenger
├── backups/                  # 0700; verified SQLite copies
└── logs/kalampeach-worker.log
```

Recommended permissions:

```bash
chmod 700 /home/CPANEL_USER/{data,private,backups,logs}
chmod 600 /home/CPANEL_USER/private/kalampeach.env
chmod 600 /home/CPANEL_USER/data/kalampeach_bot.sqlite3
```

The database, env file, backups, and logs must never be under `public_html`.
`DATABASE_PATH`/`LOG_FILE` are validated against this rule, and `backup_db.py`
refuses a public destination.

## 4. Database and migrations

- `scripts/init_db.py` creates the data directory (0700) and applies pending
  migrations from `app/migrations/` in order; it is safe to re-run and never drops
  or resets data.
- `scripts/init_db.py --show-version` prints the current schema version.
- The webhook returns 503 until the schema is initialised, so a mis-ordered
  deployment is visible instead of silently breaking writes.
- Schema version 1 creates: `schema_migrations`, `users`, `telegram_inbox`,
  `source_messages`, `events`, `clarifications`, `event_revisions`,
  `report_deliveries`.
- Always take a backup before applying a future migration.

## 5. Web requests (Passenger)

- Application root: the repository directory containing `passenger_wsgi.py`.
- Startup file: `passenger_wsgi.py` exposing `application`.
- Routes: `POST /telegram/webhook` (secret header required) and `GET /healthz`.
- The webhook validates the secret in constant time, enqueues the raw update in one
  short transaction, and returns `{"ok": true}`. It performs no AI or report work.
- A duplicate `update_id` returns success without creating a second row.

## 6. Cron worker

Preferred cPanel cron (every minute; replace paths and Python version with the real
ones shown by cPanel):

```cron
* * * * * /home/CPANEL_USER/virtualenv/APP_ROOT/3.11/bin/python /home/CPANEL_USER/APP_ROOT/scripts/run_worker.py --env-file /home/CPANEL_USER/private/kalampeach.env >> /home/CPANEL_USER/logs/kalampeach-worker.log 2>&1
```

What one invocation does (all bounded, then exits):

1. marks abandoned claims with exhausted attempts as `failed`;
2. claims up to `WORKER_BATCH_SIZE` due inbox rows with a lease;
3. processes each update, settling it durably;
4. releases any claims it did not reach;
5. evaluates due daily reports (max 2 per run) and delivers at most one per Tehran date;
6. prints a one-line JSON summary and exits 0.

Overlapping invocations are safe: leases prevent double claims, and every write path is
idempotent. If cron cannot run every minute, OpenCode must report the resulting delay.

## 7. Logs

- Default destination: stderr, captured by cPanel's error log for Passenger and by the
  cron redirect for the worker.
- Optional `LOG_FILE` for JSON lines (must be outside `public_html`).
- Logs contain stable event names, categories, IDs, durations and counts. They never
  contain message text, prompts, AI payloads, bot tokens, or API keys.
- Useful events: `update_received`, `update_processing_failed`, `event_confirmed`,
  `clarification_undeliverable`, `report_sent`, `report_delivery_uncertain`,
  `report_narrative_rejected`, `configuration_invalid`.

## 8. Backup and restore

```bash
# Backup (safe while the webhook is writing)
python scripts/backup_db.py --dest /home/CPANEL_USER/backups --env-file /home/CPANEL_USER/private/kalampeach.env
```

The script uses SQLite's online backup API, writes mode 0600, then verifies
`PRAGMA integrity_check` and row counts, printing a JSON summary.

Restore (manual, deliberate, with the worker cron paused):

```bash
crontab -l > ~/crontab.backup            # keep a copy
crontab -e                                # comment out the worker line
cp /home/CPANEL_USER/data/kalampeach_bot.sqlite3 /home/CPANEL_USER/data/pre-restore.sqlite3
cp /home/CPANEL_USER/backups/kalampeach_bot-<stamp>.sqlite3 /home/CPANEL_USER/data/kalampeach_bot.sqlite3
chmod 600 /home/CPANEL_USER/data/kalampeach_bot.sqlite3
python scripts/verify_setup.py --env-file /home/CPANEL_USER/private/kalampeach.env
# re-enable the worker cron line only after verify_setup reports integrity ok
```

Never restore over a live database without keeping a copy of the current file.

## 9. Timezone

- Business timezone `Asia/Tehran`, daily report `23:30`, database timestamps UTC.
- The worker decides dues using `zoneinfo`, not the host or cron timezone.
- Verified by tests: cutoff before/after 23:30 Tehran, missed-tick recovery, and a
  late report is marked as late.

## 10. Telegram preconditions

- BotFather privacy configured so the bot receives ordinary group text.
- Bot added to the configured group; exact group ID configured.
- Manager and employees have pressed **Start** in private chat.
- Note: a **group privacy change requires removing and re-adding the bot** to the group
  before Telegram delivers ordinary messages.
- Webhook uses `allowed_updates=["message"]` (see `scripts/set_webhook.py`).

## 11. NVIDIA preconditions

Before production, verify: correct endpoint kind, model available to the account,
Persian extraction quality on representative messages, structured-output support
(`NVIDIA_JSON_MODE`), rate limits, timeouts, and data-handling terms. A controlled
schema-valid response must be observed; failures must degrade to the documented
retry/fallback behaviour, never to a guessed event.

## 12. Deployment approval

Do not deploy, expose/register a webhook, configure cron, configure DNS, restart a
production service, or send real employee/manager messages until the owner explicitly
approves the target environment and action. Arena performed none of these steps.

## 13. Troubleshooting map

| Symptom | Likely cause | Check |
|---|---|---|
| Webhook 403 | Secret mismatch | `TELEGRAM_WEBHOOK_SECRET` vs `setWebhook` secret |
| Webhook 503 | Schema missing/outdated | `scripts/init_db.py --show-version`, application log |
| 503 `configuration_incomplete` | Missing/invalid env value | Application log lists key names |
| Updates stay `queued` | Cron not running | cron log, `run_worker.py` output |
| `TELEGRAM_FORBIDDEN` on clarification/report | User never pressed Start | Ask user to open the bot and press Start |
| `AI_UNAVAILABLE` retries then `failed` | Endpoint/model/key wrong or provider down | `verify_setup.py --network`, NVIDIA settings |
| `AI_INVALID_OUTPUT` | Model not following the JSON contract | model choice, `NVIDIA_JSON_MODE`, prompt review |
| Report `uncertain` | Send timed out after Telegram likely accepted | `report_admin.py --list`, then resolve manually |
| Report `failed` repeatedly | Manager blocked the bot / chat not started | manager private chat state |
