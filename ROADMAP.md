# Roadmap

This roadmap describes the intended MVP delivery order. It is not permission to deploy or contact real users. Keep each stage small and testable.

## Stage 0 — Requirements and repository baseline

- [x] Confirm independent project boundary.
- [x] Define one bot, one configured group, employee private chat, manager reporting.
- [x] Confirm report delivery at 23:30 Asia/Tehran.
- [x] Document scope, contracts, security, setup, and acceptance criteria.
- [ ] Confirm remaining deployment values in `DECISIONS.md`.

**Exit:** requirements are internally consistent and owner-supplied configuration is identified.

## Stage 1 — Minimal application foundation

- [ ] Select and document the simplest maintainable language/framework and runtime.
- [ ] Add configuration validation and safe startup checks.
- [ ] Add independent database, schema initialization/migrations, and UTC timestamp handling.
- [ ] Add structured logging with secret and message-content redaction.
- [ ] Add automated test setup and health/readiness checks appropriate to the chosen stack.

**Exit:** application starts safely with valid configuration and refuses unsafe/incomplete configuration clearly.

## Stage 2 — Telegram ingestion and access control

- [ ] Implement one bot and authorized group/private-chat routing.
- [ ] Enforce configured group ID and numeric user-ID allowlists.
- [ ] Implement update idempotency.
- [ ] Handle private Start onboarding and employee DM capability.
- [ ] Preserve source message metadata and original text.

**Exit:** authorized text updates are accepted once; unauthorized or unrelated updates cannot create records.

## Stage 3 — AI event extraction and persistence

- [ ] Implement NVIDIA adapter using configured endpoint/model.
- [ ] Implement strict schema-constrained extraction and validation.
- [ ] Implement business relevance classification and safe ignore behavior.
- [ ] Persist clear events and pending ambiguous events.
- [ ] Ask the original employee privately for required clarification.
- [ ] Add bounded retries and safe failure handling.

**Exit:** supported event types are stored only after validation; ambiguous cases are pending; AI outage never fabricates data.

## Stage 4 — Corrections and audit history

- [ ] Resolve Telegram replies to the originating event.
- [ ] Enforce author-or-manager correction permissions.
- [ ] Apply corrections as append-only revisions.
- [ ] Handle unresolved or multi-event reply targets with clarification.

**Exit:** corrections are attributable, auditable, and never erase prior values.

## Stage 5 — Reports

- [ ] Implement deterministic reporting queries and aggregation.
- [ ] Implement manager-only natural-language report requests.
- [ ] Implement daily report scheduling at 23:30 Asia/Tehran.
- [ ] Add duplicate-send protection and retry handling.
- [ ] Use AI only to explain/summarize computed facts.

**Exit:** daily and on-demand reports are manager-only, traceable to database facts, and safe across restarts.

## Stage 6 — Verification and release candidate

- [ ] Pass all mandatory tests in `TEST-PLAN.md`.
- [ ] Verify Telegram privacy settings and real group visibility in a controlled test.
- [ ] Verify NVIDIA structured output against representative Persian messages.
- [ ] Verify timezone, daylight-saving behavior, and scheduler restart behavior.
- [ ] Document backup/restore and operational recovery.
- [ ] Produce a final implementation report with evidence and remaining limitations.

**Exit:** owner reviews and explicitly approves any live deployment.

## Later — Not part of MVP

Potential candidates, only after separate prioritization:
- Voice-message transcription and audio input.
- Photo, receipt, and document extraction.
- Multiple groups, branches, or locations.
- Inventory alerts and low-stock thresholds.
- Charts, dashboard, exports, and scheduled report variants.
- More advanced accounting classifications and reconciliation.
- Additional AI providers or model routing.
- Role management UI and employee self-service administration.
