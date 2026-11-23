# Roadmap

This roadmap separates repository coding from later hosting operations. It is not permission to deploy or contact real users.

## Stage 0 — Requirements and repository baseline

- [x] Confirm independent project boundary.
- [x] Define one bot, one configured group, employee private chat, manager reporting.
- [x] Confirm report delivery at 23:30 Asia/Tehran.
- [x] Define Arena's GitHub-only coding role and OpenCode's later operations role.
- [x] Set cPanel shared hosting with Python as the target deployment environment.
- [x] Define webhook + durable inbox + cPanel cron architecture.
- [ ] Confirm remaining owner-supplied deployment values (bot token, group/manager/employee IDs, NVIDIA endpoint/model/key, webhook URL, currency policy).

**Exit:** requirements are internally consistent and cPanel prerequisites are documented.

## Stage 1 — Arena: application foundation in GitHub

- [x] Select and document Python version and minimal WSGI-compatible framework (Python 3.11 target / 3.9 minimum, Flask 3.0.3).
- [x] Add configuration validation and Passenger WSGI entry point (`app/config.py`, `app/wsgi.py`, `passenger_wsgi.py`).
- [x] Add independent database, schema initialization/migrations, and UTC timestamp handling (`app/db.py`, `app/migrations/`, `app/timeutil.py`).
- [x] Add durable Telegram update inbox and webhook secret validation (`app/repo/inbox.py`, `app/wsgi.py`).
- [x] Add structured logging with secret and message-content redaction (`app/logging_setup.py`).
- [x] Add automated test suite and local test instructions (`tests/`, `TEST-PLAN.md`).

**Exit:** repository contains a complete foundation designed for cPanel; runtime deployment is not claimed. **Met.**

## Stage 2 — Arena: Telegram and event workflows

- [x] Implement Telegram webhook, routing, authorization, and idempotency.
- [x] Implement cron-invoked worker and durable retry handling.
- [x] Implement NVIDIA adapter and strict extraction validation.
- [x] Implement event capture, clarification, reply-based correction, and revision history.

**Exit:** application code and automated tests cover the full event lifecycle. **Met.**

## Stage 3 — Arena: reports and code handoff

- [x] Implement deterministic reports and manager-only on-demand reports.
- [x] Implement 23:30 Asia/Tehran daily report through cron-driven due-job processing.
- [x] Add report idempotency and restart recovery.
- [x] Complete cPanel deployment guide and OpenCode handoff (`CPANEL-DEPLOYMENT.md`, `OPENCODE-HANDOFF.md`).
- [x] Update changelog, roadmap, decisions, and README.

**Exit:** code-ready repository is reviewed and handed to OpenCode. Arena does not deploy. **Met (pending owner review).**

## Stage 4 — OpenCode: cPanel inspection and deployment preparation

- [ ] Inspect actual cPanel Python versions and Passenger/WSGI support.
- [ ] Verify cron minimum interval, outbound HTTPS, webhook reachability, SQLite, storage, quotas, and process limits.
- [ ] Compare actual host capabilities with documented requirements.
- [ ] Report blockers and propose minimal compatible adjustments.
- [ ] Obtain owner approval before changing hosting settings.

**Exit:** host compatibility is evidenced and owner approves a deployment plan.

## Stage 5 — OpenCode: controlled deployment and verification

- [ ] Configure Python application and dependencies.
- [ ] Configure private environment variables and durable database path.
- [ ] Configure HTTPS Telegram webhook.
- [ ] Configure cPanel cron worker.
- [ ] Run migrations and controlled tests after backup.
- [ ] Verify group intake, private clarification, corrections, and reports using approved test accounts.
- [ ] Verify 23:30 Asia/Tehran report delivery.
- [ ] Provide operational handover and backup/restore evidence.

**Exit:** owner explicitly approves production operation after reviewing test evidence.

## Later — Not part of MVP

Potential candidates, only after separate prioritization:
- Voice-message transcription and audio input.
- Photo, receipt, and document extraction.
- Multiple groups, branches, or locations.
- Inventory alerts and low-stock thresholds.
- Charts, dashboard, exports, and scheduled report variants.
- More advanced accounting classifications and reconciliation.
- Additional AI providers or model routing.
- Role management UI and employee self-service administration.
- Jalali (Persian) calendar display in reports.
