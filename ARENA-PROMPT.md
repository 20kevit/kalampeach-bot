# Arena.ai — End-to-End Implementation Prompt

You are the implementation agent for the standalone KalamPeach Telegram Operations Bot.

## Mission

Implement the MVP described by this repository's documentation from an empty repository to a tested, runnable release candidate. Do not merely produce a plan or scaffold. Work through implementation, tests, documentation, and a final evidence-based report.

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
- `DECISIONS.md`
- `ROADMAP.md`

If any requirement conflicts or is impossible, stop and report the exact conflict. Do not silently reinterpret it.

## Absolute boundaries

1. This is a new, independent system. Do not connect to, import, inspect for reuse, or depend on the separate KalamPeach accounting website, its API, database, source, credentials, or infrastructure.
2. Do not build a website, dashboard, second bot, multi-branch system, or features listed as later/out-of-scope.
3. MVP accepts plain text only.
4. One bot, one configured group, authorized employee private chats, and one manager recipient.
5. Daily manager report time is 23:30 Asia/Tehran.
6. NVIDIA is the AI provider, accessed through a replaceable adapter.
7. Never invent API credentials, endpoint, model ID, Telegram IDs, currency, or deployment details.
8. Do not deploy, restart production services, or contact real users without explicit owner approval.

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

## Engineering requirements

- Choose a minimal maintainable stack and document the choice in `DECISIONS.md`.
- Use a dedicated relational database; SQLite is preferred for MVP unless justified otherwise.
- Use migrations or safe schema versioning. Never reset existing data automatically.
- Keep clear boundaries between Telegram, domain logic, persistence, scheduler, and AI provider.
- Implement Telegram update idempotency.
- Preserve original message text and source metadata.
- Validate AI output against a strict application-owned schema before any write.
- Use decimal-safe money handling.
- Store timestamps in UTC; use Asia/Tehran for business dates and scheduling.
- Preserve correction history.
- Make daily report delivery restart-safe and duplicate-resistant.
- Use bounded timeouts/retries and fail safely on provider/network errors.
- Do not log secrets or full private messages by default.
- Provide a deterministic report fallback if AI narrative generation fails.

## Work plan

1. Inspect repository and confirm current state.
2. Write a concise implementation plan mapped to `ROADMAP.md`.
3. Implement the minimal project foundation and configuration validation.
4. Implement database schema/migrations and tests.
5. Implement Telegram adapter, routing, authorization, and idempotency.
6. Implement NVIDIA provider adapter and strict extraction validation.
7. Implement event capture, pending clarification, and private follow-up.
8. Implement reply-based correction and append-only revisions.
9. Implement deterministic reports, AI narrative, manager authorization, and 23:30 Asia/Tehran scheduler.
10. Implement operational recovery, backup/restore instructions, and safe logging.
11. Run the full test plan and fix failures.
12. Review the complete diff for scope creep, secrets, data loss risks, and forbidden integrations.
13. Update README, CHANGELOG, ROADMAP, DECISIONS, and deployment instructions to match actual implementation.

Do not stop after a scaffold or a happy-path demo. All mandatory acceptance criteria must have corresponding automated tests where feasible.

## Handling missing configuration

The repository intentionally does not contain real credentials or owner IDs. Build configuration validation and provide placeholders in `.env.example`. The application must be runnable in test mode with mocks/fixtures, but do not create a fake live configuration or claim real Telegram/NVIDIA integration has been verified.

Do not choose a random NVIDIA model. Inspect current official NVIDIA documentation only if needed to implement the adapter; record the verified API shape and model capability. If account-specific access cannot be verified, keep it configurable and report the exact owner action required.

Do not assume a currency. Preserve unknown currency and prevent mixed/unknown currency totals.

## Test expectations

Implement tests for every section in `TEST-PLAN.md`. At minimum cover:
- Authorization and group isolation.
- Classification and event validation.
- Idempotency.
- Pending clarification and private delivery constraints.
- Reply target resolution and revision permissions.
- Decimal-safe deterministic reporting.
- Currency separation.
- Asia/Tehran report schedule and restart recovery.
- AI outage/malformed output safe behavior.
- No integration with the separate accounting app.

Use mocked Telegram/NVIDIA clients for automated tests. Real integration tests require owner-supplied credentials and explicit approval.

## Final response format

When implementation is complete, provide:
1. Summary of implemented capabilities.
2. Chosen stack and architecture rationale.
3. Repository file tree.
4. Database schema/migration summary.
5. Configuration keys still requiring owner values.
6. Exact test commands and pass/fail/skip counts.
7. Evidence of each mandatory acceptance area.
8. Known limitations and unresolved decisions.
9. Explicit statement whether any live deployment or real-user messaging occurred.
10. The exact steps the owner must perform next.

Be honest. Do not claim a test, integration, or deployment succeeded unless it was actually executed and verified.
