# Decisions and Open Configuration

This file separates confirmed product/engineering decisions from values that must be supplied before deployment.

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
| Target language | Python |
| Target hosting | Shared hosting managed through cPanel |
| Web application interface | cPanel-supported WSGI/Passenger-compatible entry point |
| Telegram delivery | HTTPS webhook; no production long polling |
| Background work | Durable database inbox plus cPanel cron-invoked worker |
| Arena responsibility | Write code/tests/docs in GitHub only; no hosting access or deployment |
| OpenCode responsibility | Later inspect, verify, deploy, and operate on cPanel after owner approval |
| MVP priority | Simplest useful implementation; defer additional features |

## Explicit MVP constraints

- Text-only input.
- One group.
- One manager identity.
- No dashboard or website.
- No external accounting integration.
- No automatic payment, POS, supplier, or banking integration.
- No dependency on always-running daemon, systemd, Docker, Redis, or root access.
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
| cPanel account details | Host plan, available Python versions, app/Passenger feature, cron capability |
| Public webhook URL | HTTPS URL mapped to the Python application |
| Backup policy | Destination, frequency, and retention |

## Implementation decisions Arena may make

Arena may select a minimal Python framework and exact schema implementation if it:
1. Confirms compatibility with cPanel's WSGI application model.
2. Chooses the lowest-complexity maintainable option.
3. Records the choice, minimum Python version, and rationale here.
4. Uses a webhook and short-lived cron worker, not long polling or a persistent daemon.
5. Does not violate any confirmed product/security/hosting requirement.
6. Does not add external paid dependencies without approval.

SQLite is the preferred MVP database if supported reliably by the actual cPanel account. Keep it in a durable directory outside the public web root.

## Open product/hosting questions

These must be resolved before the affected behavior goes live:
- Whether the manager should also be allowed to submit business events; default recommendation: yes, if their numeric ID is included in the recording allowlist.
- Whether one configured default currency may be applied when staff omit currency; until confirmed, preserve as unknown and ask or exclude from monetary totals.
- Exact NVIDIA service/model and supported structured-output mode.
- Operational retention period for original message text and audit records.
- Actual cPanel Python version, Passenger/WSGI support, cron frequency, outbound HTTPS policy, webhook reachability, SQLite support, and storage/process quotas.

Do not infer answers from the separate accounting application or its historical configuration.
