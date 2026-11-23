# Reporting Specification

## 1. Daily report

- Recipient: configured manager only.
- Schedule: every day at **23:30 Asia/Tehran**.
- Period: the current Tehran calendar day from 00:00 through the report cutoff.
- Delivery: private Telegram chat.
- The manager must have started the bot.
- Report generation and delivery state must be persisted.

Suggested sections:
1. Executive summary.
2. Recorded sales/revenue, grouped by currency.
3. Recorded expenses/purchases, grouped by currency.
4. Difference of recorded amounts only when mathematically comparable, clearly labeled as such—not automatically called profit.
5. Inventory received, consumed, wasted, or adjusted, grouped by item and unit.
6. Important incidents and operational notes.
7. Pending clarifications and data-quality caveats.
8. Record counts and report period.

## 2. On-demand report

Only the manager may request a report in natural language. Examples include:
- Today's summary.
- Sales and expenses for a specified date.
- This week's recorded activity.
- Inventory consumption for a specified period.
- Important incidents from yesterday.

The system must parse the requested report intent and date range. If the period is ambiguous, ask the manager. Do not run a broad report based on a guessed period.

## 3. Deterministic calculations

- Query only confirmed events in the requested period.
- Use application/database aggregation with decimal-safe arithmetic.
- Group amounts by currency. Never sum across currencies.
- Exclude pending/rejected records from confirmed financial totals; show their counts separately.
- Do not convert quantity units without explicit conversion rules.
- Do not infer missing transaction values.
- Every figure must be reproducible from database records.

## 4. Revenue, expenses, and profit language

The MVP reports recorded sales/revenue and recorded expenses/purchases separately.

Do not label the difference as accounting profit. It is only a difference between recorded categories and may omit inventory valuation, taxes, unpaid receivables, liabilities, and other accounting concepts.

If currency is missing and no owner-configured default resolves it, keep the record out of currency totals and disclose the missing data.

## 5. Empty or incomplete periods

- No events: say no events were recorded; do not claim there was no business activity.
- Pending events: show pending count and note that totals may be incomplete.
- AI unavailable: send a deterministic template report.
- Database query failure: do not send invented or partial figures as complete; report failure to the manager.
- If report delivery fails, persist failure and retry according to a bounded policy.

## 6. Duplicate prevention and recovery

Use a unique report key based on report type, manager ID, and Tehran local report date/period.

Persist states such as pending, sending, sent, and failed. After restart, recover due reports without sending a second copy if Telegram already accepted the original message. Where Telegram response uncertainty prevents certainty, reconcile using stored delivery metadata and a conservative retry policy; do not blindly duplicate.

Implemented behavior (schema version 2):

- The rendered text is frozen in `snapshot_json` before the first chunk is sent. A retry
  resumes at chunk `chunks_sent + 1` and always sends that frozen content, even if events
  were edited after the freeze.
- A delivery found in `sending` after a restart is resolved from its own progress:
  all chunks confirmed → `sent`; nothing attempted → safe restart; some chunks in flight →
  `failed_uncertain` with one notice to the manager, and no automatic resend.
- A missing/corrupt snapshot with confirmed chunks is never rebuilt (that would resend
  confirmed content); it stops for human review.
- A missed 23:30 tick is recovered once inside `DAILY_REPORT_RECOVERY_HOURS`. Older missed
  days are recorded as `skipped` for operator visibility and are never sent late.
- `GET /health` → `review_needed` counts the deliveries whose outcome is unknown.

## 7. AI narrative

AI receives only a precomputed fact payload. It may produce a concise Persian explanation and highlight notable recorded items. It may not recalculate totals, add unsupported numbers, or imply causation not present in the data.

A deterministic template is the fallback.
