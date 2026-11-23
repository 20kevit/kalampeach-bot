# Deployment and Configuration

This document defines required configuration, not a live deployment authorization.

## 1. Required configuration

See `.env.example`. Required values:
- Telegram bot token.
- Exact allowed Telegram group chat ID.
- Manager numeric Telegram user ID.
- Authorized employee numeric Telegram user IDs.
- NVIDIA API key, base URL, and model ID.
- Currency/default-currency policy supplied by the owner.

Do not invent values. Startup must validate required settings and fail with a clear, secret-safe message when missing.

## 2. Timezone

- Business timezone: `Asia/Tehran`.
- Daily report time: `23:30`.
- Database timestamps: UTC.
- Daily report scheduler must use an IANA timezone database, not a fixed UTC offset.
- Test behavior around timezone/DST changes and host timezone differences.

## 3. Database

- Use the independent project database.
- SQLite is the MVP preference unless implementation constraints justify a different relational database.
- Persist the database on durable storage.
- Enable foreign keys and transactional writes.
- Provide versioned schema migrations or a safe initialization mechanism.
- Never reset an existing database automatically.
- Back up before migrations and test restore.

## 4. Runtime

The implementation agent must document:
- Supported runtime and exact version.
- Installation/build commands.
- Configuration loading and validation.
- Start/stop commands.
- Health/log inspection.
- Database location and backup procedure.
- Upgrade/migration procedure.
- Recovery from NVIDIA/Telegram outages.

Prefer one process for MVP if the selected framework can reliably handle Telegram updates and the daily scheduler. If separate workers are needed, document locking and duplicate prevention.

## 5. Telegram preconditions

- BotFather privacy/group visibility configured to receive normal group text.
- Bot added to the configured group.
- Exact group ID configured.
- Manager and employees have started the bot privately.
- Numeric IDs configured and tested.
- Bot permissions limited to what is necessary.

See `TELEGRAM-SETUP.md`.

## 6. NVIDIA preconditions

Before production, verify:
- The selected NVIDIA API endpoint is correct for the chosen service.
- The configured model ID is available to the account.
- The model supports the required Persian extraction quality and output constraints.
- Rate limits, context limits, timeout behavior, and data-handling terms are understood.
- A controlled test returns schema-valid results for representative Persian text.

Never assume all NVIDIA models support identical structured-output features.

## 7. Deployment approval

Do not deploy, expose a webhook, configure DNS, restart a production service, or send real employee/manager messages until the owner explicitly approves the target environment and action.
