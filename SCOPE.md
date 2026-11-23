# Product Scope: Need / Do Not Need / Later

This document prevents MVP scope creep.

## Needed in MVP

| Capability | MVP requirement |
|---|---|
| Telegram interface | One bot |
| Group intake | Read text messages from one configured staff group |
| Private intake | Authorized employees send natural-language text to the bot |
| Business event types | Sales/revenue, expenses/purchases, inventory changes/consumption, daily incidents/notes |
| AI interpretation | Classify relevance, extract structured fields, detect missing/ambiguous required data |
| Automatic capture | Save clear, validated business events without manual approval |
| Clarification | Ask the original employee privately only when necessary |
| Non-business chat | Ignore; do not create a record |
| Corrections | Reply to the source Telegram message and describe correction in natural language |
| Audit | Preserve original message and append-only revision history |
| Identity | Store Telegram numeric sender ID, timestamp, source chat, and source message |
| Authorization | Employee allowlist for recording; manager-only reports |
| Database | Independent database owned by this project |
| Daily report | Send to manager every day at 23:30 Asia/Tehran |
| On-demand report | Manager asks in natural language |
| Reporting integrity | Deterministic totals from database; AI may summarize computed results |
| AI provider | NVIDIA API through replaceable provider adapter |
| Reliability | Idempotent Telegram processing, validation, safe retries, no guessed records |

## Explicitly not needed in MVP

- Any connection to the separate KalamPeach accounting website, its database, API, source code, or credentials.
- A website, web dashboard, admin panel, or mobile app.
- A second Telegram bot.
- Multiple Telegram groups, branches, or organizations.
- Voice, audio, video, photos, scanned receipts, or file attachments.
- Payment collection, payment gateway, bank integration, or POS integration.
- Employee attendance, shifts, payroll, HR, or performance scoring.
- Automatic stock purchasing, supplier communication, or order placement.
- Forecasting, recommendations, or AI-generated business decisions.
- Manual approval of every ordinary event.
- Arbitrary commands or a large command menu.
- Multi-provider AI routing, vector database, RAG, agents, or autonomous tool execution.
- Importing historical data from another system.
- Public reports or employee access to management reports.

## Possible later additions

These are ideas, not commitments:
- Voice and media extraction.
- Multiple groups and branch-aware reports.
- Inventory thresholds and alerts.
- Receipt/document OCR.
- CSV/PDF export and dashboard.
- More detailed accounting dimensions and cash reconciliation.
- Additional AI providers.
- Configurable report templates and delivery times.

## Scope rules

1. A feature listed under “Later” must not be implemented in MVP without owner approval.
2. A feature not listed here must be treated as out of scope until explicitly approved.
3. Simplicity does not justify losing original messages, audit history, idempotency, or financial correctness.
4. The first release is an operational event log and reporting assistant, not a full accounting system.
