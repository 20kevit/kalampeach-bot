# KalamPeach Telegram Operations Bot

An independent Telegram bot for capturing Kalam Pich business activity from one staff group and authorized private chats, storing validated events in its own database, and producing manager reports with NVIDIA AI assistance.

> **Project boundary:** This repository is a new, standalone system. It must not connect to, import from, or reuse the code, database, API, credentials, or infrastructure of the separate KalamPeach accounting web application.

## What it does

- Reads messages in one configured Telegram group and identifies business-relevant information.
- Lets authorized employees record activity in private chat using natural language.
- Captures sales/revenue, expenses/purchases, inventory changes/consumption, and daily incidents/notes.
- Saves only validated structured events; ignores ordinary conversation.
- Asks the original employee privately when required information is genuinely ambiguous.
- Allows an employee to correct a previous event by replying to the original Telegram message.
- Preserves an audit trail of every correction.
- Sends a daily report to the manager at **23:30 Asia/Tehran**.
- Lets the manager request reports in natural language at any time.

## MVP principles

- One Telegram bot, one configured group, one independent database.
- Text messages only in the first release.
- NVIDIA is the AI provider behind a replaceable provider adapter.
- The database and deterministic application code are the source of truth for all figures.
- AI may interpret text and explain results; it must never invent, calculate, or silently alter financial totals.
- Access is controlled by Telegram numeric user IDs and configured chat IDs.

## Documentation map

| File | Purpose |
|---|---|
| [AGENTS.md](AGENTS.md) | Mandatory behavior and workflow for coding agents |
| [PRODUCT-SPEC.md](PRODUCT-SPEC.md) | Functional requirements and user-facing behavior |
| [SCOPE.md](SCOPE.md) | In scope, out of scope, and later |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, boundaries, and processing flows |
| [DATA-MODEL.md](DATA-MODEL.md) | Database entities, event fields, and audit history |
| [AI-CONTRACT.md](AI-CONTRACT.md) | NVIDIA adapter and strict structured-output contract |
| [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md) | BotFather, group visibility, IDs, and private-chat setup |
| [SECURITY.md](SECURITY.md) | Authorization, privacy, secrets, and safe failure |
| [REPORTING.md](REPORTING.md) | Daily and on-demand report rules |
| [TEST-PLAN.md](TEST-PLAN.md) | Acceptance criteria and required tests |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Configuration, startup, operations, and backup expectations |
| [CPANEL-DEPLOYMENT.md](CPANEL-DEPLOYMENT.md) | cPanel shared hosting target and OpenCode deployment handoff |
| [ROADMAP.md](ROADMAP.md) | Delivery stages and future possibilities |
| [CHANGELOG.md](CHANGELOG.md) | User-visible project changes |
| [DECISIONS.md](DECISIONS.md) | Confirmed decisions and unresolved configuration |
| [ARENA-PROMPT.md](ARENA-PROMPT.md) | End-to-end implementation brief for Arena.ai |

## Before running

The deployment must provide Telegram bot credentials, the authorized group ID, manager Telegram user ID, authorized employee IDs, and NVIDIA API configuration. See [DEPLOYMENT.md](DEPLOYMENT.md) and [.env.example](.env.example).

Every employee must open the bot and press **Start** once before the bot can send them private clarification messages. The bot must be configured to receive ordinary group messages; see [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md).

## Project status

Requirements and implementation guidance are documented. Arena.ai is responsible for writing the Python application, tests, and deployment documentation in GitHub. OpenCode will later inspect the actual cPanel plan, verify compatibility, and deploy only after owner approval. See [ROADMAP.md](ROADMAP.md) for the separated build and operations stages.
