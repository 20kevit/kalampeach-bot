# Changelog

All notable project changes are recorded here. The project has not yet had an application release.

## [Unreleased]

### Changed
- Confirmed that Arena.ai is limited to code, tests, and documentation changes through GitHub.
- Assigned later cPanel environment inspection, deployment, runtime verification, and operation to OpenCode after owner approval.
- Set the deployment target to Python on shared hosting managed through cPanel.
- Replaced the assumed long-running bot/scheduler model with HTTPS Telegram webhook, durable database inbox, and cPanel cron-invoked worker.

### Added
- cPanel deployment compatibility requirements and OpenCode handoff guidance.
- Webhook secret validation, durable update queue, cron overlap, and restart-recovery acceptance criteria.

### Not yet implemented
- Telegram bot runtime and handlers.
- NVIDIA provider adapter.
- Database schema and migrations.
- Event extraction, clarification, and correction workflows.
- Daily and on-demand report generation.
- Automated test suite and cPanel deployment packaging.
