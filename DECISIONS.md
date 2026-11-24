# Decisions and Open Configuration

This file separates confirmed product/engineering decisions from values that must be supplied before deployment.

## Confirmed decisions

| Topic | Decision |
|---|---|
| Project identity | New standalone Telegram bot repository |
| Integration boundary | No connection to the separate KalamPeach accounting website, database, API, or source |
| Telegram topology | One bot |
| Group intake | One configured staff group; process relevant messages automatically |
| Private intake | Authorized employees can send natural-language messages |
| Business categories | Sales/revenue, expenses/purchases, inventory/consumption, daily incidents/notes |
| Irrelevant messages | Ignore; do not create business records |
| Ambiguity | Ask the original sender privately only when genuinely necessary |
| Corrections | Reply to original Telegram message and describe correction naturally |
| Audit | Preserve revision history |
| Reports | Daily report plus manager on-demand reports |
| Daily schedule | 23:30 Asia/Tehran |
| Daily recipient | Manager only |
| Permissions | Authorized employees record; manager receives management reports |
| Identity metadata | Telegram user ID, timestamp, source chat/message |
| AI provider | NVIDIA API |
| Provider architecture | Replaceable provider adapter |
| Target language | Python |
| Target hosting | Shared hosting managed through cPanel |
| Web application interface | cPanel-supported WSGI/Passenger-compatible entry point |
| Telegram delivery | HTTPS webhook; no production long polling |
| Background work | Durable database inbox plus cPanel cron-invoked worker |
| Arena responsibility | Write code/tests/docs in GitHub only; no hosting access or deployment |
| OpenCode responsibility | Later inspect, verify, deploy, and operate on cPanel after owner approval |
| MVP priority | Simplest useful implementation; defer additional features |

## Explicit MVP constraints

- Text-only input.
- One group.
- One manager identity.
- No dashboard or website.
- No external accounting integration.
- No automatic payment, POS, supplier, or banking integration.
- No dependency on always-running daemon, systemd, Docker, Redis, or root access.
- Deterministic database calculations; AI only interprets and summarizes.

## Values required from owner before production

| Setting | Required owner input |
|---|---|
| Telegram bot token | Secret created through BotFather |
| Telegram group ID | Numeric ID of the one staff group |
| Manager Telegram ID | Numeric ID |
| Employee Telegram IDs | Authorized numeric IDs |
| NVIDIA API key | Secret |
| NVIDIA base URL | Verified endpoint for selected NVIDIA service |
| NVIDIA model ID | Exact model identifier available to the account |
| Currency | Currency code/label and whether one default may be applied when a message omits currency |
| cPanel account details | Host plan, available Python versions, app/Passenger feature, cron capability |
| Public webhook URL | HTTPS URL mapped to the Python application |
| Backup policy | Destination, frequency, and retention |

## Implementation decisions Arena may make

Arena may select a minimal Python framework and exact schema implementation if it:
1. Confirms compatibility with cPanel's WSGI application model.
2. Chooses the lowest-complexity maintainable option.
3. Records the choice, minimum Python version, and rationale here.
4. Uses a webhook and short-lived cron worker, not long polling or a persistent daemon.
5. Does not violate any confirmed product/security/hosting requirement.
6. Does not add external paid dependencies without approval.

SQLite is the preferred MVP database if supported reliably by the actual cPanel account. Keep it in a durable directory outside the public web root.

## Implementation decisions made by Arena

These were within the "Implementation decisions Arena may make" allowance and are the
basis of the current code. They are reversible only with the same reasoning.

| Topic | Decision and rationale |
|---|---|
| Python version | Target **3.9+**, developed and tested on **3.11.2**. Only standard-library features available in 3.9 are used (`zoneinfo`, `sqlite3`, `urllib`, `hmac`). |
| Web framework | **None.** A single WSGI callable (`kalampeach/web/app.py`) is exposed by `passenger_wsgi.py`. No Flask/Django means nothing to install or version-conflict on shared hosting, and Passenger's documented contract is satisfied directly. |
| HTTP client | `urllib.request` with an injectable transport. `requests` is deliberately not used (not guaranteed installed, adds a dependency). |
| Database | SQLite in a private absolute path (`DATABASE_PATH`), WAL journal mode, `foreign_keys=ON`, 5 s busy timeout, short-lived connections, explicit `BEGIN IMMEDIATE` for writes. File permissions are tightened to `0600`. |
| Money | `Decimal` parsed from text, stored as `TEXT`. Floats are rejected at the parser boundary; there is no float arithmetic anywhere in the money path. |
| Unknown values | `NULL`, never `0`. A financial event without an amount or currency stays `pending_clarification` and is excluded from totals, with the count disclosed. |
| Currency grouping | Currencies are grouped case-insensitively (`USD` and `usd` are the same bucket) while the first-seen label is displayed. Totals are never summed across currencies, and no difference is computed without both sales and expenses in the same currency. |
| Timezone | `zoneinfo.ZoneInfo("Asia/Tehran")` (IANA data), timestamps stored as UTC `…Z` strings, business dates as `YYYY-MM-DD`. `Asia/Tehran` and `23:30` are fixed for the MVP and rejected otherwise in configuration. |
| Webhook | Validates `X-Telegram-Bot-Api-Secret-Token` with `hmac.compare_digest`, rejects missing/oversized bodies, persists the raw update to `telegram_updates` keyed by `update_id`, then returns `200 ok`. No AI call in the request path. |
| Worker | Cron-invoked `scripts/run_worker.py`. Leases + `BEGIN IMMEDIATE` claims make overlapping runs safe; work is bounded by batch size and elapsed seconds; retries use `min(60 * 2^(n-1), 1800)` with a max-attempt cap; expired leases are recovered. |
| Health | `/health` returns `200` only when the database responds **and** the shipped schema version is applied; otherwise `503` with `status: degraded`. The web process never migrates. |
| Authorization | Numeric Telegram IDs only, from configuration. Username/`@handle` is never used for authorization. The manager may record only if explicitly listed in `EMPLOYEE_TELEGRAM_IDS`. |
| Clarification | One initial question plus at most one narrower follow-up, then the event stays `pending_clarification` and the employee is told it is still open. A permanently undeliverable question is not retried forever. |
| Private-only answers | Confirmation, clarification answers/expiry, correction acknowledgements and unresolved-reply notices always go to the employee's private chat. The group receives nothing except the neutral setup hint when the bot has never been started by that employee. |
| Reports | Deterministic text assembled by `services/reporting.py`; the AI may add a narrative paragraph only. A narrative containing any number not present in the computed facts, or profit language, is discarded and the deterministic template is sent. |
| Report delivery | `report_deliveries.report_key` is UNIQUE (`daily:<manager>:<date>`, `ondemand:<id>`), chunks are recorded as they are sent, and a `failed_uncertain` send is never automatically resent. |
| Backup/restore | `scripts/backup_db.py` takes an online, verified copy and prunes old ones; restore is a documented manual procedure. The script never overwrites the live database. |
| Tests | Offline only: temporary SQLite file, scripted AI provider, recording Telegram client, controllable clock. No test can reach the network or the accounting system. |
| Schema versioning | Forward-only SQL files, SHA-256 checksummed, applied inside one `BEGIN IMMEDIATE` transaction each; a modified already-applied file is a hard error. Schema version 2 adds the clarification delivery states. |
| Clarification delivery | `pending_delivery → sending → awaiting_answer`, with `lease_owner`/`lease_expires_at_utc`. `delivery_uncertain` and a stale `sending` row are terminal for automatic sending but still answerable; only a definitely-unsent outcome is retried (`min(300 * attempts, 1800)` s). |
| Clarification reply resolution | A reply is matched by the recorded `bot_messages` mapping. When the quoted message is unknown and the sender (numeric id, private chat) has **exactly one** clarification in `sending` or `delivery_uncertain`, the reply is attributed to that question: those states are the durable evidence that a question was attempted without its message id being recorded. Two or more candidates are never guessed. The sending worker's transition is fenced by `status = 'sending' AND lease_owner`, so an answer taken mid-send wins and the delivery state is not re-applied (`answered_during_send`). |
| Report delivery | The rendered text is frozen in `snapshot_json` before the first chunk; `chunks_sent` advances per confirmed chunk; a resumed delivery never repeats a chunk and never rebuilds content; an unprovable outcome is `failed_uncertain` with one manager notice. |
| Report delivery ownership | Every transition on `report_deliveries` is fenced inside its own SQL statement: progress, finalize and release require `lease_owner = <caller>`, the interrupted-delivery settlement and the retry-exhaustion decision require an expired lease. `worker_id` is a required argument, so ownership is an explicit decision. A worker that loses the lease stops immediately, reports `lease_lost`, and never clears or overwrites the new owner's lease. |
| Missed daily reports | A bounded 30-day scan records each missed day once as `skipped` (for operator visibility) and sends only the newest day that is still inside `DAILY_REPORT_RECOVERY_HOURS`. A stale daily report is never delivered late. |
| Worker failure isolation | Intake, clarification and report phases each run in their own guard; a failure is logged, recorded in `phase_failures`, and makes the cron run exit non-zero, but does not stop the other phases. |
| Delivery review surface | `/health` → `review_needed` and `scripts/doctor.py` → `delivery_review_needed` count deliveries whose outcome is unknown. They expose counts only, never message text or IDs. |
| Python minimum enforcement | `requires-python = ">=3.9"` is checked by tests: every shipped file must parse with `feature_version=(3, 9)` and must not use 3.10+/3.11+ stdlib APIs (`tomllib`, `datetime.UTC`, `StrEnum`, `TaskGroup`, `ExceptionGroup`, `typing.Self`). |

## Second-pass decisions that need the owner's attention

These are conservative defaults chosen because the owner has not answered yet. Each is a
small, reversible change if the answer differs.

| Topic | Current behavior | Owner decision needed |
|---|---|---|
| Unknown delivery outcome | Never resent automatically; counted in `review_needed`; a manager notice is sent for reports only. | Should the manager also be notified about each uncertain clarification question (more noise, faster awareness)? |
| Manual resolution of an uncertain question | The employee can still answer it by reply; an operator can inspect counts via `/health`. There is no manual "force resend" command, by design. | Whether a documented, explicit operator command should exist at all. |
| Stale daily reports | Older missed days are recorded as `skipped` and never sent. | Whether the manager would rather receive one consolidated "these days were missed" message. |
| Retention of `skipped`/`failed_uncertain` rows | Kept indefinitely (audit value). | Retention period. |
| Manager report prompts | A reply to a manager "which period?" prompt still depends on the recorded `bot_messages` mapping (the same window exists in `_ask_for_period`). The clarification path is now durable; the manager path was left unchanged to keep this pass focused. | Whether the same attribution rule should be extended to manager period prompts. |
| Report-delivery lease renewal | The lease is not extended while a multi-chunk report is being sent. A send that outlives the lease is resolved by the next run as an unknown outcome (never duplicated, one notice) rather than resumed. | Whether a long report should renew its lease per chunk instead. |

## Current status of the owner inputs and open questions

The code is complete against the MVP scope; the table above and the questions below are
still unresolved. Until they are answered the bot runs with unknown currency preserved
(no default), the manager records only if allowlisted, and the daily report is sent at
23:30 Asia/Tehran. These defaults are deliberately conservative and reversible.

## Open product/hosting questions

These must be resolved before the affected behavior goes live:
- Whether the manager should also be allowed to submit business events; default recommendation: yes, if their numeric ID is included in the recording allowlist.
- Whether one configured default currency may be applied when staff omit currency; until confirmed, preserve as unknown and ask or exclude from monetary totals.
- Exact NVIDIA service/model and supported structured-output mode.
- Operational retention period for original message text and audit records.
- Actual cPanel Python version, Passenger/WSGI support, cron frequency, outbound HTTPS policy, webhook reachability, SQLite support, and storage/process quotas.

Do not infer answers from the separate accounting application or its historical configuration.
