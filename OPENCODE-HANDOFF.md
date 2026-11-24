# OpenCode Handoff — KalamPeach Telegram Operations Bot

Written at the end of the second development pass on branch
`arena/c55428c7-kalampeach-bot` (baseline commit `8d3f93a`) and updated by the third pass
(starting commit `234d134`). Read this file together with
[CPANEL-DEPLOYMENT.md](CPANEL-DEPLOYMENT.md) and [DECISIONS.md](DECISIONS.md).

**Nothing in this repository has been deployed, migrated on a real host, registered as a
webhook, scheduled in cron, or connected to a real Telegram bot, the NVIDIA API, cPanel or
the separate KalamPeach accounting system. cPanel compatibility is NOT verified.**

The five sections below are deliberately separated. Do not merge them into one "done"
list: (1) is already proven offline, (2)–(4) are not.

---

## 1. Implemented and tested locally (no host action needed to trust these)

Everything here is covered by an offline test that was executed in this repository on
Python 3.11.2 / SQLite 3.40.1. External calls use fakes; nothing reached the network.

| Area | Guarantee | Evidence |
|---|---|---|
| Webhook intake | HMAC-compared secret, size limit, duplicate `update_id` acknowledged once, no AI call in the request | `tests/test_webhook.py`, `tests/test_outbox.py` |
| Authorization | Numeric IDs only; group allowlist; manager-only reports in private chat; correction ownership | `tests/test_authorization.py`, `tests/test_corrections.py` |
| Clarification lifecycle | Claim before send; exactly one send under 4 concurrent workers; crash between send and bookkeeping does not resend; unknown outcome is `delivery_uncertain`, never resent, still answerable | `tests/test_clarification_delivery.py` (16 tests) |
| Clarification answers | One conditional claim decides which answer may resolve a question: two concurrent valid answers produce one revision and one confirmation; the losing answer writes nothing; a failure after the claim rolls back; a released claim is reusable | `tests/test_clarification_answer_claim.py` (27 tests) |
| Settled outcomes | `delivery_uncertain` applies only from `sending` by the current lease owner; `resolved`/`answered`/`expired`/`unable_to_deliver` are never rewritten; a rejected transition sends nothing and never resends; every owner-protected transition refuses `worker_id=None` | `tests/test_clarification_answer_claim.py` (27 tests) |
| Immediate clarification reply | A reply that arrives before the question's message id is recorded is attributed to the employee's unconfirmed question and answered; the event is confirmed, the answer applied once, and a later bookkeeping commit changes nothing | `tests/test_clarification_reply_race.py` (19 tests) |
| Report-delivery ownership | Progress, finalize and release are fenced by the lease owner in the same SQL statement; a stale worker stops, sends nothing further, and cannot clear the new owner's lease; settlement and exhaustion decisions require an expired lease | `tests/test_report_delivery_lease.py` (11 tests) |
| Report delivery | Snapshot frozen before the first chunk; resume at the first unconfirmed chunk; no repeated chunk; unprovable outcome → `failed_uncertain` + one manager notice; lost snapshot never rebuilt | `tests/test_report_delivery_safety.py` (11 tests) |
| Report schedule | Tehran business dates; 20:30 UTC = 23:30 Tehran; recovery window; multi-day gap recorded once per day and never sent late | `tests/test_reports.py`, `tests/test_reporting.py` |
| Worker | Bounded batch and elapsed time; lease claims; a worker that lost its lease cannot finish or requeue; phases isolated | `tests/test_worker.py` |
| Migrations | 001 + 002, checksummed, one transaction each; modified historical migration rejected; five concurrent `ensure_schema` calls apply each version once; failing migration rolls back; 001→002 keeps ids/timestamps/id sequence | `tests/test_migrations.py` (9 tests) |
| Integrity | Money as `Decimal`/TEXT, FK enforcement, `0600` file mode, WAL, secrets absent from repo and logs, declared Python 3.9 minimum enforced by AST parsing | `tests/test_integrity.py`, `tests/test_decimal_time.py` |
| Operator visibility | `/health` → `review_needed` (counts); `doctor.py` → `delivery_review_needed` | `tests/test_integrity.py`, `tests/test_reports.py` |

Full suite: `python3 -m pytest` → **352 passed in 8.66s** (250 at the end of the first
pass, +45 in the second pass, +30 in the third pass, +27 in the fourth pass; no existing
test was modified or relaxed). No lint/static tool is configured in this repository (no
ruff/flake8/mypy config, no CI workflow); the checks actually executed are:

```bash
python3 -m pytest
python3 -m compileall -q kalampeach scripts passenger_wsgi.py
python3 scripts/migrate.py --status / --apply            # disposable DB
python3 scripts/doctor.py                                # disposable DB
python3 scripts/run_worker.py                            # disposable DB
python3 scripts/backup_db.py --output /tmp/…/backups     # disposable DB
```

---

## 2. Needs a read-only cPanel inspection (report evidence, change nothing)

OpenCode must collect and report the following before any write operation. These are
unknown to this repository and must not be assumed.

| # | Question | How to answer (read-only) | Why it matters |
|---|---|---|---|
| 1 | Which Python versions are available, and which will the app use? | "Setup Python App" screen; `python --version` inside the virtualenv | `requires-python = ">=3.9"`; code is AST-checked against 3.9 but only executed on 3.11 here |
| 2 | Is Passenger / "Setup Python App" present, and what is the documented entry point? | Screenshot or copy of the Application Root / Startup File fields | The code exposes `application` from `passenger_wsgi.py`; a different startup file name would need a one-line change |
| 3 | Is there outbound HTTPS to `api.telegram.org` and to the configured NVIDIA base URL? | `curl -sS -o /dev/null -w '%{http_code}' https://api.telegram.org` | Both are required; no fallback exists |
| 4 | Is `zoneinfo` + tzdata usable (`Asia/Tehran` resolves)? | `python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Asia/Tehran'))"` | Business dates and the 23:30 schedule depend on it |
| 5 | What is the SQLite version and the filesystem of the data directory? | `python -c "import sqlite3; print(sqlite3.sqlite_version)"`; `df -T <dir>` | WAL needs a local filesystem; NFS-style locking is a real risk |
| 6 | Are concurrent writes from the webhook and cron possible, and what is the busy timeout behaviour? | Observe/measure; report the actual filesystem | Lock behaviour is the main SQLite risk on shared hosting |
| 7 | What are cron minimum interval, max runtime, and output/mail behaviour? | Cron Jobs screen + host documentation | The worker assumes one run per minute and exits in seconds |
| 8 | How are environment variables exposed to Passenger and to cron? | Test a trivial script under both | The app reads `ENV_FILE`/`environ`; cron often has a different environment |
| 9 | Is the application restarted automatically after a code change? | Touch a file and observe; or use the documented restart action | Stale Passenger workers served old code in known cPanel setups |
| 10 | Disk quota, memory limit, process limit, max request time | Account statistics | The suite is small, but SQLite + backups consume quota |
| 11 | Does the account's TLS certificate cover the chosen hostname, and does HTTPS reach the app? | `curl -sSI https://HOST/health` | Telegram requires HTTPS on 443/80/88/8443 |

**Do not state "cPanel compatible" as a conclusion.** State the evidence row by row.

---

## 3. Owner-provided configuration (nothing can be invented here)

| Setting | Where it goes | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | private env / `.env` outside `public_html` | BotFather secret |
| `TELEGRAM_GROUP_ID` | same | Numeric, negative |
| `MANAGER_TELEGRAM_ID` | same | Numeric user ID |
| `EMPLOYEE_TELEGRAM_IDS` | same | Numeric, comma-separated; include the manager only if they may record |
| `TELEGRAM_WEBHOOK_SECRET` | same | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `WEBHOOK_PUBLIC_URL` | same | Must match the real HTTPS route |
| `NVIDIA_API_KEY`, `NVIDIA_BASE_URL`, `NVIDIA_MODEL` | same | No default model is invented |
| `DATABASE_PATH` | same | Absolute, outside `public_html` |
| `DEFAULT_CURRENCY` (optional) | same | Empty means "ask/exclude", never zero |
| Backup destination + retention | owner policy | `backup_db.py --output --keep` |
| Retention for message text / audit rows | owner policy | Currently kept indefinitely |

Every employee must press **Start** in the bot once, otherwise private questions are
undeliverable (the code records `unable_to_deliver` and posts one neutral setup hint in
the group).

---

## 4. Needs explicit owner approval before execution

These actions change the real world. None has been performed.

1. Creating the cPanel Python application and uploading the code to the account.
2. Creating the data/log/backup directories and setting permissions.
3. Running `scripts/migrate.py --apply` against the real `DATABASE_PATH` (it writes).
4. Registering the webhook: `python scripts/telegram_webhook.py set --yes` (contacts
   Telegram and changes bot state). Also `info` and `delete` variants.
5. Creating the cron entry that runs the worker every minute.
6. Sending any message to the real group, manager or employees — including the acceptance
   script in `TEST-PLAN.md` section H.
7. Restoring a backup over a live database.
8. Any change to the NVIDIA account (model availability, quota, key rotation).

---

## 5. Exact command sequences

Paths are placeholders: replace `CPANEL_USER`, `APP_ROOT`, `PYVER` with the real values
reported in section 2. All commands assume the working directory is `APP_ROOT` unless
stated otherwise.

### 5.1 Install (after approval)

```bash
cd /home/CPANEL_USER/APP_ROOT
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/pip install -r requirements.txt
# runtime requirements.txt is intentionally empty; requirements-dev.txt has pytest only
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python -c "import kalampeach, sqlite3; print('ok')"
mkdir -p /home/CPANEL_USER/data /home/CPANEL_USER/logs /home/CPANEL_USER/backups
chmod 700 /home/CPANEL_USER/data /home/CPANEL_USER/backups
chmod 600 /home/CPANEL_USER/kalampeach.env      # private env file, never under public_html
```

### 5.2 Migration (backup first; reads are safe, `--apply` writes)

```bash
cd /home/CPANEL_USER/APP_ROOT
export ENV_FILE=/home/CPANEL_USER/kalampeach.env
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/backup_db.py \
    --output /home/CPANEL_USER/backups --keep 14        # skip on a brand-new empty target
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/migrate.py --status
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/migrate.py --dry-run
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/migrate.py --apply
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/migrate.py --status
```

Expected: `001 initial_schema applied`, `002 clarification_delivery_states applied`.
The 002 migration rebuilds `clarifications` in place (ids, timestamps, FK and the id
sequence are preserved). There is no downgrade; restoring the pre-upgrade backup is the
rollback.

### 5.3 Health checks (read-only)

```bash
cd /home/CPANEL_USER/APP_ROOT
export ENV_FILE=/home/CPANEL_USER/kalampeach.env
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/doctor.py
curl -sS -i https://HOST/health
```

Acceptable result:

- `doctor.py`: `Result: 0 failure(s), 0 warning(s)`, including
  `[PASS] delivery_review_needed — none` and `[PASS] migrations — up to date`.
- `/health`: `HTTP/1.1 200 OK`, body with `"status": "ok"`, `"schema_version": 2`,
  `"expected_schema_version": 2`, and `"review_needed": {…, "total": 0}`.

`503` with `"status": "degraded"` means the database is unreachable or not migrated —
do not register the webhook in that state.

### 5.4 Cron (after approval)

```cron
* * * * * /home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python /home/CPANEL_USER/APP_ROOT/scripts/run_worker.py >> /home/CPANEL_USER/logs/kalampeach-worker.log 2>&1
```

The worker prints one JSON line per run. Exit `0` = clean; exit `1` = a phase failed
(`"phase_failures"` names it) while the other phases still ran. Watch
`delivery_review_needed` and `review_needed.total`: a non-zero value means a delivery
outcome is unknown and needs a human.

### 5.5 Webhook registration (owner approval required; do not run blind)

```bash
cd /home/CPANEL_USER/APP_ROOT
export ENV_FILE=/home/CPANEL_USER/kalampeach.env
PY=/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python
$PY scripts/telegram_webhook.py info            # read-only
$PY scripts/telegram_webhook.py set --yes       # changes the real bot
$PY scripts/telegram_webhook.py info            # confirm url + pending_update_count
```

### 5.6 Acceptance tests

Offline suite (safe, no network) on the account's interpreter:

```bash
cd /home/CPANEL_USER/APP_ROOT
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/pip install -r requirements-dev.txt
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python -m pytest
```

Expect `325 passed`. If the count differs, report the exact number and the failing names —
do not report "tests passed" generically.

Live acceptance (`TEST-PLAN.md` section H) requires the owner's test group and explicit
approval; it is the only way to verify real Telegram delivery, and it has never been run.

### 5.7 Backup

```bash
cd /home/CPANEL_USER/APP_ROOT
export ENV_FILE=/home/CPANEL_USER/kalampeach.env
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/backup_db.py \
    --output /home/CPANEL_USER/backups --keep 14
ls -l /home/CPANEL_USER/backups        # expect kalampeach_bot_<UTC>.sqlite3, mode 0600
```

The script verifies the copy (`integrity=ok`) and refuses any destination inside
`public_html`.

### 5.8 Restore (destructive; owner approval required)

```bash
# 1. Disable the cron entry.
# 2. Stop writes (no cron, no webhook traffic ideally).
export ENV_FILE=/home/CPANEL_USER/kalampeach.env
cd /home/CPANEL_USER/APP_ROOT
cp /home/CPANEL_USER/data/kalampeach_bot.sqlite3 /home/CPANEL_USER/backups/pre_restore_$(date -u +%Y%m%dT%H%M%SZ).sqlite3
cp /home/CPANEL_USER/backups/kalampeach_bot_<TIMESTAMP>.sqlite3 /home/CPANEL_USER/data/kalampeach_bot.sqlite3
chmod 600 /home/CPANEL_USER/data/kalampeach_bot.sqlite3
rm -f /home/CPANEL_USER/data/kalampeach_bot.sqlite3-wal /home/CPANEL_USER/data/kalampeach_bot.sqlite3-shm
# 3. Verify before re-enabling anything:
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/migrate.py --status
/home/CPANEL_USER/virtualenv/APP_ROOT/PYVER/bin/python scripts/doctor.py
curl -sS -i https://HOST/health
# 4. Re-enable cron.
```

The backup script never overwrites the live database; the copy step above is manual and
deliberate.

---

## Known limitations and remaining risks

1. **No host verification.** Every hosting claim in this repository is a design
   expectation, not a measurement.
2. **SQLite locking on shared hosting.** The code uses WAL, `foreign_keys=ON`, a 5 s busy
   timeout, short-lived connections and `BEGIN IMMEDIATE` for writes. If the actual
   filesystem cannot support that, the run fails loudly (`DatabaseError`) rather than
   silently corrupting; the fallback decision belongs to the owner.
3. **Unknown delivery outcomes need a human.** By design there is no automatic resend and
   no "force resend" command. The operational path is: read `review_needed`, read the
   matching log line, let the employee reply or the manager re-request the report.
4. **Exactly-once is per-attempt, not per-eternity.** Telegram offers no idempotency key;
   the code guarantees "never blind-resend", not "impossible to duplicate under any
   network lie".
5. **A long report send can outlive its lease.** The delivery lease is not renewed per
   chunk, so if a multi-chunk send takes longer than `WORKER_LEASE_SECONDS`, the next cron
   run settles the delivery as an unknown outcome (no duplicate, one manager notice)
   instead of resuming it. If this happens in practice, raise `WORKER_LEASE_SECONDS`
   (owner decision in `DECISIONS.md`), not the code.
6. **Manager period prompts still depend on the mapping.** An immediate reply to an
   on-demand report's "which period?" prompt can hit the same window that clarifications
   now survive. It was deliberately left unchanged in the third pass; see
   `DECISIONS.md`.
7. **One answer wins, the other is ignored.** Two valid answers to the same question are
   no longer applied twice, but the superseded answer is silently dropped (source row
   `ignored` / `answer_claim_lost`) rather than merged. This is the pre-existing product
   rule — a question has exactly one authoritative answer — and the losing message is
   still visible in the database for audit. If the owner ever wants conflicting answers
   reconciled, that is a product change, not a defect.
8. **Clock and timezone.** The schedule is correct only if the host has a working
   `Asia/Tehran` tz database; a missing tzdata makes startup fail rather than silently
   shifting the report.
9. **Two-person review not performed.** A single agent pass, however many tests pass, is
   not a production-readiness claim.
10. **Sections H (human acceptance) remains unexecuted.**
11. **No lint/static analysis is configured** in the repository; adding one is a decision
    for the owner, not a hidden step.

---

## What was explicitly NOT done

- No cPanel/hosting access, no SSH to a production host, no file upload.
- No deployment, no service start, no Passenger restart.
- No webhook registration or modification; no Telegram API call of any kind.
- No NVIDIA API call; no credential used.
- No cron entry created or modified.
- No production or owner database touched, migrated, backed up or restored.
- No message sent to any real Telegram user, group or bot.
- No contact with, import from, or connection to the separate KalamPeach accounting
  website/database/API.
- No release, tag, deploy, merge to `main`, or interaction with the other agent's branch.
