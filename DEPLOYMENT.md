# Deployment and Configuration

The target deployment is a Python application on shared hosting managed through cPanel. Arena.ai prepares the code and documentation only. OpenCode will later inspect and deploy it after the owner approves the operational steps.

## 1. Required configuration

See `.env.example`. Required values:
- Telegram bot token.
- Exact allowed Telegram group chat ID.
- Manager numeric Telegram user ID.
- Authorized employee numeric Telegram user IDs.
- NVIDIA API key, base URL, and model ID.
- Currency/default-currency policy supplied by the owner.
- Public HTTPS webhook base URL and a strong webhook secret token.

Do not invent values. Startup must validate required settings and fail with a clear, secret-safe message when missing.

## 2. Target hosting model

- Python application through cPanel's supported Python application feature (commonly Setup Python App with Passenger).
- WSGI-compatible application entry point.
- Telegram webhook over HTTPS.
- Durable database-backed inbox.
- cPanel Cron Jobs to process updates and scheduled reports.
- No long polling, persistent daemon, systemd, Docker, Redis, or root access assumed.

See `CPANEL-DEPLOYMENT.md` for the exact target model and later OpenCode handoff.

## 3. Timezone

- Business timezone: `Asia/Tehran`.
- Daily report time: `23:30`.
- Database timestamps: UTC.
- The worker must evaluate due jobs using an IANA timezone database, not the host's default timezone or a fixed UTC offset.
- cPanel cron may run in a different timezone; schedule the worker frequently and let application code determine whether 23:30 Asia/Tehran has arrived.
- Test behavior around timezone/DST changes and host timezone differences.

## 4. Database

- Use the independent project database.
- SQLite is the MVP preference if supported by the actual hosting account.
- Persist the database under the account home directory, outside the public web root.
- Enable foreign keys and transactional writes.
- Provide versioned schema migrations or a safe initialization mechanism.
- Never reset an existing database automatically.
- Back up before migrations and test restore.
- Avoid concurrent long writes; keep webhook and worker transactions short.

## 5. Runtime and cPanel checks

OpenCode must inspect and record:
- Available Python versions in cPanel.
- Whether the account exposes Python application setup/Passenger and the supported WSGI interface.
- Whether cron jobs can run at one-minute intervals.
- Whether outbound HTTPS requests to Telegram and the selected NVIDIA API are allowed.
- Whether inbound HTTPS webhooks can reach the application URL.
- SQLite module availability and durable writable storage.
- Available disk quota, memory/process limits, and application restart behavior.

Do not claim compatibility until these checks are performed on the actual hosting plan.

## 6. Telegram preconditions

- BotFather privacy/group visibility configured to receive normal group text.
- Bot added to the configured group.
- Exact group ID configured.
- Manager and employees have started the bot privately.
- Numeric IDs configured and tested.
- Bot permissions limited to what is necessary.
- Webhook URL is public HTTPS and webhook secret is configured.

See `TELEGRAM-SETUP.md`.

## 7. NVIDIA preconditions

Before production, verify:
- The selected NVIDIA API endpoint is correct for the chosen service.
- The configured model ID is available to the account.
- The model supports the required Persian extraction quality and output constraints.
- Rate limits, context limits, timeout behavior, and data-handling terms are understood.
- A controlled test returns schema-valid results for representative Persian text.

Never assume all NVIDIA models support identical structured-output features.

## 8. Deployment approval

Do not deploy, expose/register a webhook, configure cron, configure DNS, restart a production service, or send real employee/manager messages until the owner explicitly approves the target environment and action.
