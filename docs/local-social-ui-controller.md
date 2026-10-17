# Local social UI controller (no social APIs)

The `social_control` action uses Mark-LV's existing `browser_control` Edge extension bridge for browser mode and local Windows UI Automation (`pywinauto`) for installed-app mode. Desktop screenshots are not sent to a vision service. It does not call social network APIs, read app databases, extract cookies, or access credentials.

## Supported actions

- `open`: open the platform in the existing Edge UI, or request launching the installed application with `mode=app`.
- `inspect`: in browser mode, inspect visible page text only after validating HTTPS and an exact host allowlist; in `mode=app` or `mode=desktop`, inspect the local Windows accessibility tree.
- `prepare_message`: searches for the requested contact through the visible UI, selects the result, checks the active conversation header and exact draft, and prepares a draft; it does not send. Desktop mode requires readable Windows UI Automation controls.
- `send_message`: routes through Mark-LV's human confirmation gate. After approval it re-checks the visible page, prepares the draft, clicks the platform's Send control and checks whether the exact text appears in visible page text. This is not proof of delivery, server receipt, or read status.
- `start_call`, `start_video_call`, `end_call`: human-confirmed UI attempts. UI labels differ by app version; the result explicitly does not claim a call connected unless verified.
- `capabilities`: report the controller's scope.

Platforms configured: WhatsApp Web, Telegram Web, Discord, Instagram Direct, Messenger, Slack and Microsoft Teams.

## Safety and limits

- Sign-in is manual. No password, session token, cookie or private API is used.
- Browser mode uses the existing Edge bridge and its visible-text heuristics; generic page text cannot always distinguish a conversation header from other mentions, so users must verify the conversation in the app UI. Desktop mode uses the local Windows accessibility tree and fails closed if it cannot identify the active conversation header or read back the exact draft.
- Side effects require the existing visible human confirmation UI. If that confirmation UI is not bound/available, the request fails closed.
- Desktop mode is local-only and requires the installed app to expose usable UI Automation controls. Unsupported/ambiguous controls and unverified states stop the operation. Browser mode's `smart_type`/`smart_click` remain accessibility-label heuristics.
- Group management, membership changes, bulk messaging, accepting calls automatically, and background/hidden-message access are not implemented. They should not be claimed as supported.
- UI automation is only tested at the unit/mock level in this PR; live sessions require manual validation with each app and current app version. Native installed-app interaction currently targets Windows; other operating systems must use browser mode.
