# Local social UI controller (no social APIs)

The `social_control` action uses Mark-LV's existing `browser_control` Edge extension bridge and `open_app` allowlist. It does not call social network APIs, read app databases, extract cookies, or access credentials.

## Supported actions

- `open`: open the platform in the existing Edge UI, or request launching the installed application with `mode=app`.
- `inspect`: inspect visible page text only after validating HTTPS and an exact host allowlist.
- `prepare_message`: searches for the requested contact through the visible UI, selects the result, re-checks the page text, and prepares a draft; it does not send.
- `send_message`: routes through Mark-LV's human confirmation gate. After approval it re-checks the visible page, prepares the draft, clicks the platform's Send control and checks whether the exact text appears in visible page text. This is not proof of delivery, server receipt, or read status.
- `start_call`, `start_video_call`, `end_call`: human-confirmed UI attempts. UI labels differ by app version; the result explicitly does not claim a call connected unless verified.
- `capabilities`: report the controller's scope.

Platforms configured: WhatsApp Web, Telegram Web, Discord, Instagram Direct, Messenger, Slack and Microsoft Teams.

## Safety and limits

- Sign-in is manual. No password, session token, cookie or private API is used.
- The controller searches for the contact, selects a visible result, and checks the resulting page text before composing. Because generic page text cannot reliably distinguish a conversation header from other visible mentions, users must verify the selected conversation in the confirmation prompt and in the app UI.
- Side effects require the existing visible human confirmation UI. If that confirmation UI is not bound/available, the request fails closed.
- The current bridge's `smart_type`/`smart_click` are accessibility-label heuristics, not stable app-specific DOM selectors. App UI changes, unsupported labels, bridge failures and unverified states are reported as errors.
- Group management, membership changes, bulk messaging, accepting calls automatically, and background/hidden-message access are not implemented. They should not be claimed as supported.
- UI automation is only tested at the unit/mock level in this PR; live sessions require manual validation with each app and current browser version.
