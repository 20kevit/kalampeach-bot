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

## Security acceptance

The security-related cases in `TEST-PLAN.md` are mandatory release gates.
