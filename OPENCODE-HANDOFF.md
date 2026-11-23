# OpenCode Handoff — cPanel Inspection, Setup, Verification

**Status of this document:** instructions for the later operations phase. Arena has
not executed any step below. Nothing here has been verified against a real cPanel
account, Telegram, or NVIDIA.

**Precondition:** the owner has reviewed the repository and explicitly approves the
target environment and each live action (deployment, webhook, cron, test messages).

---

## 0. Absolute boundaries for the operations phase

- One bot, one configured group, authorized employee private chats, one manager.
- No connection to the separate KalamPeach accounting website/database/API.
- No second bot, no website/dashboard, no multi-branch logic.
- Text-only intake (media is refused/ignored by the application).
- Do not invent IDs, model names, endpoints, currency, or hosting capabilities.
- Do not send messages to real staff/manager until approved test accounts exist.
- Never print the bot token or NVIDIA key in logs, chat, tickets or screenshots.

---

## 1. Read-only inspection (before any change)

Record actual evidence for each item; do not infer from marketing pages.

| # | Check | Command / place | Evidence to record |
|---|---|---|---|
| 1 | Python versions available | cPanel → Setup Python App | exact versions offered |
| 2 | Passenger/WSGI app support | same page | whether `passenger_wsgi.py` + `application` is the required interface |
| 3 | Application root path | cPanel app settings | absolute path |
| 4 | Virtualenv + interpreter path | cPanel shows it | exact `.../bin/python` path |
| 5 | Package install method | venv pip | succeeds for `Flask`, `requests`, `tzdata` |
| 6 | Cron minimum interval | cPanel → Cron Jobs | whether `* * * * *` is allowed |
| 7 | Cron execution limits | host docs/support | max runtime/output |
| 8 | Inbound HTTPS route + TLS | browser/curl to the app URL | valid certificate, path mapping |
| 9 | Outbound HTTPS | from the venv: `python -c "import requests;print(requests.get('https://api.telegram.org',timeout=10).status_code)"` | reachable |
| 10 | SQLite module + version | `python -c "import sqlite3;print(sqlite3.sqlite_version)"` | version ≥ 3.24 (older works too; `ON CONFLICT` needs 3.24+) |
| 11 | Filesystem locking / WAL | create a test DB in `data/`, open two connections | journal mode accepted, locking works |
| 12 | Disk quota / memory / process limits | cPanel stats | numbers |
| 13 | Env vars for Passenger vs cron | cPanel env UI + a cron probe | do they match? (docs recommend the private env file) |
| 14 | Restart behaviour after file changes | cPanel | automatic or manual restart needed |

If any of #2, #6, #9, #10, #11 fails, **stop and report** with the observed evidence
and the smallest compatible option. Do not silently switch to long polling, a daemon,
or a new hosted service.

---

## 2. Owner inputs required before setup

None of these exist in the repository; placeholders are intentionally empty.

1. `TELEGRAM_BOT_TOKEN` — from BotFather.
2. `TELEGRAM_GROUP_ID` — numeric ID of the one staff group (usually negative).
3. `MANAGER_TELEGRAM_ID` — numeric manager user ID (manager must press Start).
4. `EMPLOYEE_TELEGRAM_IDS` — comma-separated numeric IDs (each must press Start).
5. `TELEGRAM_WEBHOOK_SECRET` — generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
6. `WEBHOOK_PUBLIC_URL` — exact HTTPS URL, e.g. `https://example.com/telegram/webhook`.
7. `NVIDIA_API_KEY`.
8. `NVIDIA_BASE_URL` — verified OpenAI-compatible chat-completions base URL.
9. `NVIDIA_MODEL` — exact model ID enabled for the account (and whether JSON mode is supported → `NVIDIA_JSON_MODE`).
10. `DATABASE_PATH` — absolute private path, e.g. `/home/USER/data/kalampeach_bot.sqlite3`.
11. Currency policy — the currency label and whether a default may be applied (`DEFAULT_CURRENCY`), or leave empty to ask employees.
12. Backup destination/frequency/retention.
13. Whether the manager may record events (`MANAGER_CAN_RECORD_EVENTS`).
14. Confirmation that group privacy is configured so the bot receives ordinary group text, and that the bot was **removed and re-added** after the privacy change if applicable.

---

## 3. Setup steps (after approval)

```bash
# paths below are placeholders — replace with the inspected real ones
APP=/home/USER/APP_ROOT
PY=/home/USER/virtualenv/APP_ROOT/3.11/bin/python
ENV=/home/USER/private/kalampeach.env

# 1. directories and permissions
mkdir -p /home/USER/{data,private,backups,logs}
chmod 700 /home/USER/{data,private,backups,logs}

# 2. private env file (never commit; never under public_html)
$PY - <<'PY'   # or create it with the cPanel file manager
# write KEY=VALUE lines from values supplied by the owner
PY
chmod 600 "$ENV"

# 3. dependencies (production set only)
cd "$APP" && $PY -m pip install -r requirements.txt

# 4. schema (idempotent, non-destructive)
$PY scripts/init_db.py --env-file "$ENV"

# 5. secret-free diagnostics (must print no problems)
$PY scripts/verify_setup.py --env-file "$ENV"
$PY scripts/verify_setup.py --env-file "$ENV" --network   # optional connectivity probe

# 6. restart the Passenger app, then probe the public endpoint
curl -sS https://example.com/healthz    # expect {"status":"ok", "schema_version":1, ...}
```

## 4. Cron setup

```cron
* * * * * /home/USER/virtualenv/APP_ROOT/3.11/bin/python /home/USER/APP_ROOT/scripts/run_worker.py --env-file /home/USER/private/kalampeach.env >> /home/USER/logs/kalampeach-worker.log 2>&1
```

Verify: one manual run prints `{"worker": {...}}` and exits 0; the log has no secrets
and no message text.

## 5. Webhook registration (live change; owner approval required)

```bash
$PY scripts/set_webhook.py --info --env-file "$ENV"      # inspect first
$PY scripts/set_webhook.py --confirm --env-file "$ENV"   # register
$PY scripts/set_webhook.py --info --env-file "$ENV"      # verify no last_error_message
```

`allowed_updates` is set to `["message"]`. Then send one approved test message and
confirm an inbox row appears with status `queued`/`done`.

## 6. Automated tests on the host

The suite is offline by design (fake AI/Telegram, temporary SQLite) and needs no
credentials. Install the dev dependency only in a scratch venv or temporarily:

```bash
cd "$APP"
$PY -m pip install -r requirements-dev.txt
$PY -m pytest -q
```

Record the exact counts reported. If installing pytest on production is unacceptable,
run the suite in a separate test directory/venv and remove it afterwards. Do not
install dev dependencies into the production venv permanently without owner consent.

## 7. Controlled acceptance tests (approved test accounts only)

Follow `TEST-PLAN.md` §H and record observed results:

1. employee sends a clear sale → exactly one confirmed event + short group ack;
2. employee sends ordinary chatter → no event, no bot message;
3. employee sends an ambiguous expense → private question → answer → confirmed;
4. employee corrects by replying to the source message → revision 2, previous snapshot retained;
5. unauthorised account and a second group → ignored, no AI call;
6. manager asks for a date-range report → report delivered privately;
7. daily report due-job proven without waiting for 23:30 (controlled fixture/clock);
8. backup + restore drill with integrity output.

## 8. Operational handover to the owner

Provide: paths, the cron line, log locations, how to read `telegram_inbox` statuses,
how to inspect report deliveries (`scripts/report_admin.py --list`), how to resolve an
`uncertain` report, the backup/restore procedure, known limitations below, and every
test result actually observed (with counts).

## 9. Known limitations to communicate

- Text only; media is refused/ignored.
- Gregorian ISO dates in reports (no Jalali conversion).
- No currency-synonym normalization; `تومان` and `IRT` are separate totals.
- Group privacy must be set correctly and the bot re-added; otherwise group text is
  not delivered.
- Users must press Start before private questions/reports can be delivered.
- A timed-out report send stays `uncertain` until an operator resolves it (by design,
  to avoid duplicate reports).
- If cron cannot run every minute, update processing and report delivery are delayed.
- `REPORT_CATCHUP_DAYS` bounds how far back missed daily reports are recovered, and
  empty past days are not backfilled.

## 10. Definition of done for the operations phase

- Inspection evidence recorded; no unsupported capability assumed.
- Configuration complete and `verify_setup.py` clean on the real host.
- Schema initialised; health endpoint 200 over HTTPS.
- Cron worker running every minute with clean logs.
- Webhook registered with the secret and verified.
- Controlled acceptance tests executed and reviewed by the owner.
- Backup and restore verified.
- Owner explicitly approves production operation.
