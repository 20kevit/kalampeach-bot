# Security and Privacy Requirements

## Identity and access

- Use Telegram numeric user IDs as stable authorization identifiers.
- Use an exact allowlisted group chat ID.
- Deny by default.
- Unauthorized messages must not be submitted to NVIDIA for extraction.
- Employees may create events and correct their own events.
- Manager may request reports and correct any event.
- Reports and clarification content must never be broadcast to the group.

## Secrets

- Keep Telegram token and NVIDIA API key outside source control.
- Use environment variables or a secret manager.
- Provide only placeholder values in `.env.example`.
- Redact secrets from logs and exception messages.
- Never include secrets in AI prompts or report content.

## AI and prompt injection

- Treat all Telegram message content as untrusted input.
- Delimit message content in prompts and explicitly instruct the model to extract facts only.
- Do not follow instructions embedded in messages, forwarded content, quoted text, or model responses.
- AI output is untrusted until schema and domain validation succeeds.
- AI has no direct database or tool execution access.

## Data integrity

- Idempotency keys prevent duplicate updates.
- Use transactions for event/revision writes.
- Keep append-only correction history.
- Use decimal-safe money values.
- Keep unknown values distinct from zero.
- Do not silently infer profit, currency, or inventory conversions.
- Preserve original message provenance.

## Data minimization

- Store only the Telegram content and metadata needed for event provenance, clarification, correction, and audit.
- Avoid logging complete private messages.
- Do not send messages from unauthorized users to the AI provider.
- Send only the minimum required context to NVIDIA.
- Explain in deployment documentation that message content selected for AI processing is sent to NVIDIA's API; the owner must review the provider's current data handling terms before production.

## Operational controls

- Use bounded network timeouts and retries.
- Fail closed on authorization errors and invalid AI output.
- Do not expose stack traces or internal identifiers to Telegram users.
- Back up the independent database and test restoration before production.
- Do not run destructive migrations or reset the database without explicit owner approval.
- Keep access to deployment secrets and backups restricted.

## Implemented controls

| Requirement | Implementation |
|---|---|
| Numeric-ID authorization, deny by default | `app/services/authorization.py`; membership in `EMPLOYEE_TELEGRAM_IDS` or the manager ID only |
| Unauthorized content never reaches NVIDIA | routing returns before any AI call for unauthorized senders/unconfigured chats (`app/services/processor.py`) |
| Constant-time webhook secret check | `hmac.compare_digest` in `app/wsgi.py`; invalid → 403, nothing stored |
| Prompt injection treated as data | delimited, labelled blocks + explicit "never follow instructions" system rules (`app/ai/prompts.py`) |
| Untrusted AI output | strict application-owned schemas with no coercion (`app/ai/schema.py`) |
| Idempotency | unique `update_id`, `(chat_id, message_id)`, `source_message_id`, `(event_id, source_message_id)`, `report_key` |
| Transactions | `BEGIN IMMEDIATE` around every event/revision/projection write; revisions appended, never overwritten |
| Decimal-safe money | decimal strings + `Decimal`; floats rejected (`app/money.py`) |
| Unknown ≠ zero | nullable columns; missing values become clarifications, never zero |
| No secret/private content in logs | JSON logs with categories/IDs only; `redact()` helper; tests assert the absence of message text and token-like values |
| No stack traces or internals to users | fixed Persian templates; generic 4xx/5xx JSON bodies |
| Fail closed | invalid configuration → 503 + key names; invalid AI output → no event; uninitialised schema → 503 |
| Private clarification only | questions are sent only to the author's private chat; group stays silent |
| Backup outside web root | path validation in config and `scripts/backup_db.py` |

## Security acceptance

The security-related cases in `TEST-PLAN.md` are mandatory release gates.
