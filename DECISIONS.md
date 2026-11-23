# Decisions and Open Configuration

This file separates confirmed product decisions from values that must be supplied before deployment.

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
| MVP priority | Simplest useful implementation; defer additional features |

## Explicit MVP constraints

- Text-only input.
- One group.
- One manager identity.
- No dashboard or website.
- No external accounting integration.
- No automatic payment, POS, supplier, or banking integration.
- Deterministic database calculations; AI only interprets and summarizes.

## Values required from owner before production

| Setting | Required owner input |
|---|---|
| Telegram bot token | Secret created through BotFather |
| Telegram group ID | Numeric ID of the one staff group |
| Manager Telegram ID | Numeric ID |
| Employee Telegram IDs | Authorized numeric IDs |
| NVIDIA API key | Secret |
| NVIDIA base URL | Verified endpoint for selected NVIDIA service |
| NVIDIA model ID | Exact model identifier available to the account |
| Currency | Currency code/label and whether one default may be applied when a message omits currency |
| Deployment target | Host/runtime and durable database location |
| Backup policy | Destination, frequency, and retention |

## Implementation decisions Arena may make

Arena may select a minimal framework, runtime, and exact schema implementation if it:
1. Compares reasonable simple options briefly.
2. Chooses the lowest-complexity maintainable option.
3. Records the choice and rationale here.
4. Does not violate any confirmed product/security requirement.
5. Does not add external paid dependencies without approval.

SQLite is the preferred MVP database unless a concrete technical constraint justifies another relational database.

## Open product questions

These do not block documentation or initial implementation scaffolding, but must be resolved before the affected behavior goes live:
- Whether the manager should also be allowed to submit business events; default recommendation: yes, if their numeric ID is included in the recording allowlist.
- Whether one configured default currency may be applied when staff omit currency; until confirmed, preserve as unknown and ask or exclude from monetary totals.
- Exact NVIDIA service/model and supported structured-output mode.
- Operational retention period for original message text and audit records.

Do not infer answers from the separate accounting application or its historical configuration.
