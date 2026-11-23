# Arena.ai — Repository Implementation Prompt

You are the coding agent for the standalone KalamPeach Telegram Operations Bot.

## Your exact responsibility

You can write and modify project code and documentation in the connected GitHub repository. Your assignment is to prepare a complete, cPanel-compatible Python application and its tests.

**You are not the deployment or operations agent.** The later deployment, hosting inspection, runtime verification, Telegram webhook registration, cron setup, and live operation will be assigned to OpenCode separately.

Do not attempt to access cPanel, SSH, hosting accounts, production databases, live Telegram chats, or real credentials. Do not deploy or start the live bot. Do not claim that hosting or runtime behavior has been verified.

## Mission

Implement the MVP described by this repository's documentation from the current repository state to a complete code-ready handoff. Do not merely produce a plan or scaffold. Implement the application, automated tests, configuration templates, migrations, and detailed cPanel deployment instructions in GitHub.

## Mandatory reading

Read all of these before coding:
- `README.md`
- `AGENTS.md`
- `SCOPE.md`
- `PRODUCT-SPEC.md`
- `ARCHITECTURE.md`
- `DATA-MODEL.md`
- `AI-CONTRACT.md`
- `TELEGRAM-SETUP.md`
- `SECURITY.md`
- `REPORTING.md`
- `TEST-PLAN.md`
- `DEPLOYMENT.md`
- `CPANEL-DEPLOYMENT.md`
- `DECISIONS.md`
- `ROADMAP.md`

If any requirement conflicts or is impossible, stop and report the exact conflict. Do not silently reinterpret it.

## Absolute boundaries

1. This is a new, independent system. Do not connect to, import, inspect for reuse, or depend on the separate KalamPeach accounting website, its API, database, source, credentials, or infrastructure.
2. Do not build a website/dashboard, second bot, multi-branch system, or features listed as later/out-of-scope.
3. MVP accepts plain text only.
4. One bot, one configured group, authorized employee private chats, and one manager recipient.
5. Daily manager report time is 23:30 Asia/Tehran.
6. NVIDIA is the AI provider, accessed through a replaceable adapter.
7. The application must target Python on cPanel shared hosting, using a cPanel-supported WSGI application entry point and cron-invoked background jobs.
8. Use Telegram webhooks, not long polling or an assumed always-on process, for the shared-hosting deployment target.
9. Never invent API credentials, endpoint, model ID, Telegram IDs, currency, hosting capabilities, or deployment details.
10. Do not deploy, access hosting, configure live webhooks/cron, or contact real users. OpenCode handles the later operational phase after owner approval.

## Required product behavior

Implement all requirements in `PRODUCT-SPEC.md`, including:
- Group and private natural-language event capture.
- Event types: sale/revenue, expense/purchase, inventory movement/consumption, operational note.
- Automatic save for clear validated events.
- Ignore irrelevant conversation.
- Private clarification to the original sender for genuine ambiguity.
- Reply-based natural-language corrections.
- Append-only revision/audit history.
- Employee/manager authorization by numeric Telegram ID.
- Daily manager report and manager-only on-demand reports.
- Deterministic database totals; AI may only narrate computed facts.

## Hosting-compatible engineering requirements

- Use Python. Select a minimal framework that can run as a WSGI application under the target cPanel Python application/Passenger facility; record the exact choice and minimum Python version.
- Provide the expected Passenger WSGI entry point (for example, `passenger_wsgi.py`) and document the cPanel application root and startup configuration.
- Implement Telegram webhook handling over HTTPS. Validate Telegram's webhook secret token header.
- Webhook handler must durably enqueue the update and return promptly; do not perform long AI processing in the webhook request.
- Provide a cron-invoked worker command that drains durable queued updates, handles retries, and runs due report jobs.
- Design for cPanel cron invocation at least once per minute; all jobs must be idempotent and safe under overlapping invocations.
- Do not depend on Celery, Redis, Docker, systemd, root access, a persistent daemon, or a separate queue service for MVP.
- Use SQLite by default if the actual hosting filesystem and Python SQLite module support it; keep its file in a durable, non-public path under the account home directory. Document a minimal fallback only if hosting constraints make SQLite unusable.
- Keep runtime dependencies compatible with the selected cPanel Python version and document pinned dependencies.
- Include safe migrations, backup/restore instructions, log paths, and permission requirements.

## Work plan

1. Inspect repository and confirm current state.
2. Write a concise implementation plan mapped to `ROADMAP.md`.
3. Choose the Python runtime/framework compatible with cPanel's WSGI app support and record the decision.
4. Implement configuration validation, Passenger WSGI entry point, and webhook secret verification.
5. Implement durable Telegram update inbox, idempotency, and cron worker.
6. Implement independent database schema/migrations.
7. Implement Telegram routing, authorization, and update processing.
8. Implement NVIDIA provider adapter and strict extraction validation.
9. Implement event capture, pending clarification, and private follow-up.
10. Implement reply-based correction and append-only revisions.
11. Implement deterministic reports, AI narrative, manager authorization, and cron-triggered 23:30 Asia/Tehran delivery.
12. Implement recovery, backup/restore instructions, and safe logging.
13. Write automated tests for the full `TEST-PLAN.md`.
14. Review the complete diff for scope creep, secrets, data-loss risks, forbidden integrations, and cPanel incompatibilities.
15. Update README, CHANGELOG, ROADMAP, DECISIONS, and deployment instructions to match the actual implementation.
16. Prepare an OpenCode handoff section with exact cPanel inspection, setup, test, webhook, cron, and deployment steps. Do not execute those steps.

## Handling missing configuration

The repository intentionally does not contain real credentials or owner IDs. Build configuration validation and provide placeholders in `.env.example`. Automated tests must use mocks/fixtures and run without live Telegram or NVIDIA credentials.

Do not choose a random NVIDIA model. Keep endpoint/model configurable and report the exact owner action required.

Do not assume a currency. Preserve unknown currency and prevent mixed/unknown currency totals.

## Test expectations

Create tests for every section in `TEST-PLAN.md`, at minimum:
- Authorization and group isolation.
- Classification and event validation.
- Idempotency and durable webhook inbox.
- Webhook secret validation and fast acknowledgement.
- Cron worker overlap, recovery, and retry handling.
- Pending clarification and private delivery constraints.
- Reply target resolution and revision permissions.
- Decimal-safe deterministic reporting and currency separation.
- Asia/Tehran report schedule and restart recovery.
- AI outage/malformed output safe behavior.
- No integration with the separate accounting app.

Arena must not claim tests passed if it cannot execute them. If tests are not executable in its environment, provide exact commands and mark results as not run.

## Final response format

When repository implementation is complete, provide:
1. Summary of code implemented.
2. Chosen Python version/framework and cPanel compatibility rationale.
3. Repository file tree.
4. Database schema/migration summary.
5. Configuration keys still requiring owner values.
6. Automated tests added and exact commands to run later; clearly distinguish authored from executed tests.
7. Known limitations and unresolved decisions.
8. Explicit statement that no cPanel deployment, live webhook, cron setup, or real-user messaging occurred.
9. OpenCode handoff checklist and the exact owner inputs needed next.

Be honest. Do not claim a test, integration, or deployment succeeded unless it was actually executed and verified.
