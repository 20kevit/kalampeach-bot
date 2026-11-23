# Product Specification

## 1. Purpose

Build a small Telegram assistant for Kalam Pich staff. It captures operational and financial facts expressed in natural Persian text, stores them in a dedicated database, and gives the manager reliable daily and on-demand reports.

This is an event-capture and reporting MVP, not a full accounting application.

## 2. Actors and permissions

### Employee
- Must be listed by Telegram numeric user ID.
- Can submit text in the configured staff group or private chat.
- Can clarify a pending event.
- Can correct their own event by replying to its source message.
- Cannot request management reports unless also configured as manager.

### Manager
- Identified by a single configured Telegram numeric user ID.
- Can submit events like an employee if also included in the employee allowlist, or may be granted recording permission explicitly by the configuration.
- Can request reports in natural language.
- Receives the daily report at 23:30 Asia/Tehran.
- Can correct any event.
- Must not be inferred from username or group title.

### Unauthorized user
- Cannot create, modify, or read business records.
- Messages from unauthorized users must not be sent to the AI provider for business extraction.
- Ignore or respond with a generic access-denied message without revealing configuration.

## 3. Supported event categories

The first release supports:

1. **Sale / revenue:** a sale, amount received, or revenue fact.
2. **Expense / purchase:** money spent, supplier purchase, or operating expense.
3. **Inventory movement / consumption:** item received, used, wasted, or adjusted, with quantity and unit where known.
4. **Operational incident / daily note:** issue, repair, shortage, customer-related incident, or important daily explanation.

Do not force every message into one of these categories. If a message is ordinary conversation or has no business value, ignore it.

The implementation must preserve the distinction between sale/revenue and expense/purchase. Do not label purchases as sales or infer profit from incomplete facts.

## 4. Message processing

### 4.1 Group messages
1. Accept only messages from the configured group.
2. Verify sender authorization before AI processing.
3. Ignore bot messages, service messages, unsupported media, and unauthorized senders.
4. For authorized text, classify whether it contains a business event.
5. Ignore irrelevant conversation.
6. Extract structured data for relevant content.
7. Validate extracted data.
8. If sufficiently clear, save automatically and associate it with the source Telegram message.
9. If a required field is genuinely ambiguous or missing, store a pending candidate and ask the original author in private chat.
10. Never post sensitive clarification details into the group.

### 4.2 Private messages
- Only authorized employees and the configured manager may use the business intake workflow.
- Parse natural-language text using the same event contract.
- If the message is non-business, do not create an event.
- Ask concise follow-up questions only for required ambiguities.
- The bot may send a private message only after the user has opened the bot and pressed Start.

### 4.3 Supported input
MVP supports plain text only. For photo, voice, audio, video, or document messages, do not claim to have understood or recorded their contents. Respond with a short notice that this input type is not supported yet, if the sender is authorized and a response is appropriate.

## 5. Required data quality

- Never invent an amount, item, quantity, unit, date, or employee.
- Preserve the original message text.
- Record who sent the source message, when, and from which chat/message.
- Distinguish unknown from zero and missing from not applicable.
- Currency must be explicit in the event or resolved from an owner-configured default. Until the owner configures a currency, do not silently assume one.
- For inventory, preserve the unit exactly as stated; do not convert units without an explicit deterministic conversion rule.
- Relative dates must be interpreted in Asia/Tehran and normalized to an unambiguous date. Ask if the reference date is unclear.
- If a required field is unclear, ask instead of guessing.

## 6. Clarification lifecycle

- A candidate event with unresolved required fields has status `pending_clarification`.
- The bot asks the original author privately and records the question/response linkage.
- A valid answer updates the pending candidate and creates an auditable revision/clarification record.
- Once all required fields are clear and validated, the event becomes `confirmed`.
- If the employee cannot clarify or the content is not an event, mark it `rejected` or `ignored` with a reason; do not delete the source message record.
- Do not ask multiple redundant questions when one concise question can resolve the ambiguity.

## 7. Corrections

- An employee replies to the original Telegram source message with a natural-language correction.
- The system resolves the replied-to `chat_id + message_id` to the event or pending candidate.
- The author may correct their own event; the manager may correct any event.
- If the reply target maps to multiple events or cannot be resolved, ask for clarification; do not guess.
- Apply a correction as a new revision with actor, timestamp, original values, new values, and reason/source text.
- The current event view reflects the latest accepted revision; the full history remains available.
- A correction that changes event category or financial meaning must be validated as carefully as a new event.

## 8. Reports

- Daily report is sent to the configured manager at 23:30 Asia/Tehran.
- Manager may request a date range or a specific report in natural language.
- Report sections should include recorded sales/revenue, expenses/purchases, inventory movements/consumption, and important incidents.
- Show totals only when the underlying records contain comparable, explicit currency values.
- Keep currencies separate; never add different currencies together.
- Do not call sales minus expenses “profit” unless the owner explicitly defines the accounting basis and all relevant inputs are available. Prefer “recorded sales/revenue”, “recorded expenses”, and “difference of recorded amounts” with a clear caveat if shown.
- State when data is incomplete, pending, or unavailable.
- No records means “no events were recorded in this period”, not proof that no business activity occurred.
- AI can write a readable narrative from computed data but cannot calculate or invent figures.

## 9. Time handling

- Store timestamps in UTC.
- Interpret business dates and daily report boundaries in `Asia/Tehran`.
- Daily report period is the local calendar day ending at 23:30 delivery time; report includes events assigned to that Tehran calendar date up to the report cutoff.
- Scheduler must be timezone-aware and resilient to process restarts.
- If a report cannot be delivered at 23:30, retry safely and avoid sending duplicates. Report late delivery accurately.

## 10. User experience

- Use concise, natural Persian for employee interaction and reports.
- Confirm a successful save with a short acknowledgment when helpful; do not flood the group with confirmations.
- Clarification goes privately to the original author.
- Group messages unrelated to business should not trigger bot chatter.
- Do not expose internal prompts, raw AI JSON, database IDs, secrets, or stack traces to users.

## 11. MVP completion

MVP is complete only when all mandatory acceptance criteria in `TEST-PLAN.md` pass and the owner-provided deployment configuration is complete.
