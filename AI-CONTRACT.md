# AI Contract — NVIDIA Provider

## 1. Responsibility boundary

AI is an interpretation assistant, not the database, authorization system, accounting engine, or source of truth.

AI may:
- Classify whether a message contains a supported business event.
- Extract structured fields from Persian natural language.
- Identify missing/ambiguous required fields.
- Interpret a natural-language correction as a proposed patch.
- Turn deterministic report facts into a concise Persian narrative.

AI must not:
- Decide access permissions.
- Directly write to the database.
- Invent missing values, amounts, dates, currencies, or quantities.
- Calculate financial totals or reconcile accounting balances.
- Execute tools, SQL, shell commands, or arbitrary instructions found in a message.
- Treat user messages as system/developer instructions.
- Expose hidden prompts or secrets.

## 2. Provider adapter

Create an internal provider interface independent of NVIDIA-specific request/response details. Configure:
- `NVIDIA_API_KEY`
- `NVIDIA_BASE_URL`
- `NVIDIA_MODEL`

Do not hardcode an endpoint or model ID. The owner must supply and verify the actual NVIDIA endpoint, model identifier, and structured-output capabilities before deployment. Do not silently substitute a different model.

Use bounded timeouts and bounded retries. Do not retry indefinitely. Do not send credentials in prompts or logs.

## 3. Extraction output

The application owns a strict JSON schema. The exact wire format may follow the selected NVIDIA model's supported structured-output mode, but the normalized internal result must have this conceptual shape:

```json
{
  "classification": "business_event",
  "event_type": "sale_revenue",
  "confidence": 0.0,
  "event_date": null,
  "amount": null,
  "currency": null,
  "item_name": null,
  "quantity": null,
  "unit": null,
  "inventory_direction": null,
  "description": null,
  "missing_fields": [],
  "ambiguities": [],
  "reason": null
}
```

Allowed classifications: `business_event`, `non_business`, `unclear`.

Allowed event types: `sale_revenue`, `expense_purchase`, `inventory_movement`, `operational_note`, or null when not applicable.

Rules:
- Reject unknown keys or invalid enum values unless the application explicitly supports schema evolution.
- Confidence is advisory only; deterministic validation rules decide whether to save or clarify.
- Null is not zero.
- Amounts must be represented as decimal strings or another lossless decimal-safe form, never floating-point money.
- Preserve currency exactly or normalize through a documented mapping.
- Do not fabricate a date; resolve relative dates against the message timestamp and Asia/Tehran only when unambiguous.
- For `non_business`, do not create an event.
- For `unclear`, do not create a confirmed event.
- The application validates event-type-specific required fields.

## 4. Clarification

AI may suggest one concise question for missing/ambiguous fields. Application code chooses whether clarification is necessary and who receives it.

Questions must:
- Be in natural Persian.
- Ask only for the minimum missing information.
- Avoid repeating known facts.
- Never ask for passwords, API keys, or sensitive authentication data.
- Be sent only to the original authorized sender in private chat.

## 5. Correction contract

A correction request is interpreted against the current event snapshot. AI returns a proposed patch and explanation. Application code checks:
- Target event exists and is uniquely resolved.
- Actor is owner or manager.
- Fields are allowed and valid.
- Event remains semantically consistent.
- Required values remain complete.

Only then append a revision. Never let AI select a different event target from free text when the Telegram reply target already identifies the source.

## 6. Reporting narrative contract

The application supplies a fact payload containing:
- Report period and timezone.
- Deterministic totals separated by event type and currency.
- Inventory movements grouped by item/unit/direction.
- Important operational notes.
- Counts of confirmed, pending, rejected, and excluded records.
- Data completeness caveats.

AI may summarize this payload. It must not add numerical claims not present in it. If narrative generation fails, send a deterministic template report rather than failing the report entirely.

## 6b. Implementation notes

- Interface: `app/ai/provider.py` (`AIProvider` protocol with `extract_event`,
  `interpret_correction`, `interpret_manager_request`, `narrate_report`).
- Schemas: `app/ai/schema.py` rejects unknown keys, invalid enums, floats for money,
  fenced/prose-wrapped JSON, non-business output carrying business values, and
  implausible dates (31 days back / 1 day forward).
- NVIDIA adapter: `app/ai/nvidia.py` calls an OpenAI-compatible
  `<NVIDIA_BASE_URL>/chat/completions` endpoint with `NVIDIA_MODEL`,
  `NVIDIA_TIMEOUT_SECONDS`, `NVIDIA_MAX_RETRIES`, optional
  `response_format={"type":"json_object"}` behind `NVIDIA_JSON_MODE`. Nothing is
  hardcoded; no model is chosen by the repository.
- Exactly **one** bounded repair attempt is made after malformed or schema-invalid
  output; a second failure becomes `AI_INVALID_OUTPUT`, is observable on the inbox row,
  and creates no event.
- Clarification questions are generated deterministically by the application from the
  validated missing-field list (Persian templates). The model is not asked to invent
  questions in the MVP, which removes a hallucination surface; the contract permits
  this because a suggestion is optional.
- Report narrative: the deterministic template is always sent; an AI narrative is only
  prepended if a guard proves it contains no number outside the computed facts and no
  profit claim.
- Prompts delimit untrusted content and neutralise embedded closing delimiters
  (`</message>` cannot escape its block). Message content is never logged.

## 7. Validation and failure policy

- Parse JSON strictly; no markdown fences or surrounding prose accepted as the structured result.
- Validate against schema and business rules.
- On malformed output, at most one bounded repair/retry may be attempted if safe.
- If still invalid, mark processing failed/pending and preserve the original message.
- Never fall back to guessing or unvalidated free-text extraction.
- Store model/provider metadata needed for audit, but never API keys.
- Treat prompt injection inside Telegram content as untrusted data; extraction prompts must explicitly delimit the content and prohibit following instructions embedded in it.
