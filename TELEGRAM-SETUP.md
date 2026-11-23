# Telegram Setup

## 1. Create the bot

1. Create one bot through the official BotFather account.
2. Store the token only in the deployment secret store or environment.
3. Never commit the token to Git.
4. Configure a recognizable bot name and description.
5. Do not create a second bot for reports.

## 2. Allow the bot to read group messages

The bot must receive ordinary text messages from the one configured staff group.

- Configure BotFather group privacy so the bot can receive ordinary group messages, or use an appropriately privileged bot configuration that provides the required visibility.
- Add the bot to the intended staff group.
- If group administrator privileges are required for the selected configuration, grant only the minimum necessary.
- Record the exact numeric group chat ID as `TELEGRAM_GROUP_ID`.
- Test with ordinary employee text messages, not only commands or messages that mention the bot.
- Reject updates from every other group.

Do not assume that merely adding a bot to a group makes all group messages visible.

## 3. Employee private-chat onboarding

Telegram bots cannot initiate a private conversation with a user who has never started the bot.

Every authorized employee must:
1. Open the bot's private chat.
2. Press **Start**.
3. Send the onboarding message if the bot requires it.
4. Be added to the authorized numeric user-ID list by the owner.

This is required so the bot can privately ask that employee for clarification.

## 4. IDs and authorization

- Obtain numeric Telegram user IDs and group chat ID using a trusted bot update/debug method during setup.
- Configure `TELEGRAM_GROUP_ID`, `MANAGER_TELEGRAM_ID`, and `EMPLOYEE_TELEGRAM_IDS`.
- Do not authorize by username, display name, phone number, or group membership alone.
- Do not publish IDs unnecessarily in public documentation.
- Ensure the manager has started the bot privately to receive daily reports.

## 5. Controlled test

Before production:
- Send a normal text message in the group from an authorized employee.
- Verify the bot receives it without a mention or command.
- Send an unrelated message and verify no event is created.
- Send a clear business event and verify one event is created.
- Send an ambiguous event and verify clarification is sent privately to the author.
- Verify the bot does not post private clarification content in the group.
- Verify a message from another group or unauthorized user is ignored.
- Verify the manager receives a test report only after explicit approval for the test.

## 6. Operational safety

- Do not share the bot token in screenshots, logs, prompts, or source control.
- If the token is exposed, revoke/rotate it through BotFather and update deployment secrets.
- Do not use a personal user account or userbot; use the single bot account.
