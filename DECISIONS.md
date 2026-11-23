# Decisions and Open Configuration

This file separates confirmed product/engineering decisions from implementation choices and values that must be supplied before deployment.

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

## Implementation choices made by Arena

These were required to be recorded here by AGENTS.md §4 and DECISIONS.md.

| Topic | Choice and rationale |
|---|---|
| Python version | **Target 3.11, minimum 3.9.** `zoneinfo` (3.9+) provides the IANA timezone handling the report schedule needs; 3.11 is the common cPanel version family. OpenCode must record the exact version available on the account. |
| Web framework | **Flask 3.0.3** — minimal WSGI framework with no server of its own, so cPanel Passenger owns the process lifecycle. Chosen over Django (far too heavy) and a hand-rolled WSGI router (more code to review for a security-sensitive endpoint). |
| WSGI entry point | `passenger_wsgi.py` exposing module-level `application`, delegating to `app.wsgi`. Passenger application root = repository root. |
| HTTP client | `requests` 2.32.3 with explicit timeouts everywhere; no async runtime. |
| Timezone data | `tzdata` pinned so a host with a minimal system tz database still resolves `Asia/Tehran`. |
| Database | **SQLite** via stdlib `sqlite3`. WAL requested but optional; `busy_timeout=5000`, `foreign_keys=ON`, short `BEGIN IMMEDIATE` transactions. No ORM (explicit reviewable SQL). |
| Schema versioning | Append-only numbered SQL files in `app/migrations/` plus a `schema_migrations` table. Version 1 is the initial schema. Migrations never drop or reset data. |
| Money representation | Canonical decimal **strings** with `Decimal` arithmetic; floats rejected at every boundary. Totals are computed in Python from stored rows so they are reproducible and exact. |
| Money storage columns | `TEXT` for `amount`/`quantity`; grouping and summation happen in code. Chosen over integer minor units because the MVP must not assume a currency's decimal scale. |
| Event type nullable while pending | `events.event_type` is `NULL` only while a candidate's category is unconfirmed, so a genuinely ambiguous message can be stored and clarified instead of guessed. A confirmed event always has a valid type (enforced by `validate_semantics`). |
| Idempotency layers | `telegram_inbox.update_id UNIQUE`, `source_messages(chat_id, message_id) UNIQUE`, `events.source_message_id UNIQUE`, `event_revisions(event_id, source_message_id) UNIQUE`, `report_deliveries.report_key UNIQUE`. |
| Queue and worker | Inbox rows have status, attempts, `available_at`, and a **lease** (`claim_token`, `claimed_until`) so overlapping cron runs cannot double-process. Worker runtime and batch size are bounded and configurable. |
| Claim accounting | `attempts` increments when a row is claimed, so a crash loop cannot retry forever; abandoned claims with exhausted attempts become `failed` with category `CLAIM_EXPIRED` (observable). |
| Unprocessed claims | Claims left when the runtime budget is hit are released back to `retry` immediately instead of waiting for the lease. |
| Clarification questions | Generated **deterministically** from the validated missing-field list (Persian templates). The model is not asked to invent questions, which removes a prompt-injection and hallucination surface. AI-contract §4 says the model *may* suggest; it is not required. |
| Clarification answer routing | 1) reply to the bot's question message; 2) otherwise a private message when the employee has exactly one open clarification; 3) otherwise ask the employee to reply. Managers are routed through report-intent classification first. |
| Clarification rounds | `MAX_CLARIFICATION_ROUNDS` (default 3). After that the candidate becomes `rejected` with a reason and the employee is told privately that it was not recorded; nothing is guessed. |
| Undeliverable private question | If Telegram refuses (employee never pressed Start), the clarification becomes `unable_to_deliver`, the event stays pending, and nothing is posted in the group. |
| Correction target | Resolved exclusively from the Telegram reply target (`chat_id` + `message_id`); the model never selects an event. Patch fields are restricted to `MUTABLE_FIELDS`; status/history are not patchable. |
| Revision numbering | `event_revisions` is append-only with `(event_id, revision_number)` unique; the projection update and revision insert share one transaction. |
| Report cutoff | The daily report for Tehran date *D* covers events with `event_date = D` whose `confirmed_at_utc <= cutoff(D 23:30 Asia/Tehran)`. This keeps a later clarification from retroactively changing a report already sent. |
| Report catch-up | `REPORT_CATCHUP_DAYS` (default 1) bounds recovery. A past day is only recovered automatically when it has at least one confirmed record; a first-deploy empty "no events" report is not generated. Today's report is always attempted at its cutoff. |
| Report delivery states | `pending → sending → sent` or `failed` (retryable, bounded) or `uncertain` (send timed out; **not** retried automatically because Telegram may have accepted it). `scripts/report_admin.py` resolves `uncertain` manually. |
| Report idempotency | `report_key = daily:<tehran-date>:<manager-id>` or `on_demand:<chat-id>:<message-id>`, plus a delivery lease. |
| Report narrative | The deterministic Persian template is **always** sent; an AI narrative may be prepended only if a guard proves it introduced no number outside the computed facts and no profit claim. Otherwise the template alone is sent. |
| Report dates | Gregorian ISO (`YYYY-MM-DD`). Jalali conversion is not implemented in the MVP (no dependency, no silent approximation); listed under "Later" in ROADMAP. |
| Currency policy | Currency labels are preserved verbatim (whitespace-normalized). `DEFAULT_CURRENCY` fills a gap only when the owner configures it. No currency-synonym mapping (e.g. `تومان` vs `IRT`) is applied; totals are grouped by the exact label so distinct labels can never be merged incorrectly. |
| Group acknowledgements | A short reply confirmation is posted for confirmed events (`SEND_GROUP_ACKNOWLEDGEMENTS`, default true). Failures to send an ack never fail the saved event. |
| Unsupported media | Private chats get a short notice; groups stay silent to avoid chatter. Media is never sent to the AI provider. |
| Manager recording | The manager can record only when listed in `EMPLOYEE_TELEGRAM_IDS` or when `MANAGER_CAN_RECORD_EVENTS=true`. Default is `false`; DECISIONS' recommendation was "yes if listed", which the allowlist satisfies. |
| Manager private routing | "Report" vs "event" vs "small talk" is decided by one narrow AI intent call before capture. Non-business stays silent; event goes to capture when recording is allowed, otherwise a neutral private notice. |
| Authorization source | Configuration (numeric IDs) only. The `users` table stores observed display names for audit and never grants access. |
| Webhook failure responses | Invalid secret → 403 with no detail; out-of-date schema → 503; storage failure → 503 (Telegram retries); malformed payload → 400. Acknowledged only after durable storage or duplicate confirmation. |
| Startup failure | Missing/invalid configuration produces a clear log listing key **names** and a 503 webhook response (`ConfigErrorApp`) rather than an opaque crash or a partially working bot. `scripts/verify_setup.py` exits non-zero. |
| Logging | Single-line JSON; never message text, prompts, AI payloads, tokens or keys. Exception logs record the type and a stable category. |
| Error categories | Stable strings (`AI_UNAVAILABLE`, `AI_INVALID_OUTPUT`, `TELEGRAM_FORBIDDEN`, `DELIVERY_UNCERTAIN`, `DOMAIN_VALIDATION`, `SCHEMA_ERROR`, …) persisted on queue/report rows. |
| Out-of-range AI values | Implausible dates (more than 31 days back / 1 day forward) and invalid money are rejected as invalid AI output; the source message is preserved and no event is fabricated. |
| `ALLOW_MESSAGE_DATE_AS_EVENT_DATE` | Default **false**: an unstated date triggers a private question. The owner can opt in to using the message's Tehran date. |
| Backups | SQLite online backup API into a private directory, mode 0600, with `PRAGMA integrity_check` and row counts; restore is a documented manual copy (no script that can overwrite live data). |
| Dependencies | Flask, requests, tzdata only (pytest for development). No Celery, Redis, Docker, systemd, APScheduler, or external queue. |

## Values required from owner before production

| Setting | Required owner input | Status |
|---|---|---|
| Telegram bot token | Secret created through BotFather | **Not supplied** |
| Telegram group ID | Numeric ID of the one staff group | **Not supplied** |
| Manager Telegram ID | Numeric ID | **Not supplied** |
| Employee Telegram IDs | Authorized numeric IDs | **Not supplied** |
| Telegram webhook secret | High-entropy random string (16–256 chars, `A-Za-z0-9_-`) | **Not supplied** — generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| Public webhook URL | HTTPS URL mapped to the Python application | **Not supplied** |
| NVIDIA API key | Secret | **Not supplied** |
| NVIDIA base URL | Verified OpenAI-compatible chat-completions base URL | **Not supplied** |
| NVIDIA model ID | Exact model identifier available to the account | **Not supplied** |
| NVIDIA JSON mode | Whether the selected model supports `response_format={"type":"json_object"}` | **Not supplied** (default false) |
| Currency | Currency label and whether one default may be applied when omitted | **Not supplied** (default empty = ask) |
| cPanel account details | Host plan, Python versions, Passenger feature, cron capability | **Not inspected** (OpenCode) |
| Backup policy | Destination, frequency, retention | **Not supplied** |

No placeholder, default, or guessed value for any of these is present in the code.

## Open product/hosting questions

These must be resolved before the affected behavior goes live:
- Whether the manager should also be allowed to submit business events (implemented as configuration; default off unless listed).
- Whether one configured default currency may be applied when staff omit currency (implemented; default off).
- Exact NVIDIA service/model and supported structured-output mode.
- Operational retention period for original message text and audit records.
- Actual cPanel Python version, Passenger/WSGI support, cron frequency, outbound HTTPS policy, webhook reachability, SQLite support, and storage/process quotas.
- Whether Persian (Jalali) dates are required in reports (not in MVP).
- Whether currency-label normalization is desired (not in MVP).

Do not infer answers from the separate accounting application or its historical configuration.
