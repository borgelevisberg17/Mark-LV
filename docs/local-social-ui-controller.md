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

## Scenario coverage and recommended operating flows

### Preferred automation ladder

1. **Browser mode first when the service is already signed in:** validate the exact HTTPS host before reading the page. Prefer accessible role/name or label-based DOM locators with a unique match, visibility/enabled/stability checks, and read-back. The current Edge bridge is the available adapter; its smart labels are heuristic and are not equivalent to a full Playwright locator engine.
2. **Installed Windows app mode:** target a uniquely identified app window, inspect its UI Automation tree, prefer native UIA patterns (Invoke/Value/Selection) over simulated clicks and keystrokes, and re-resolve elements immediately before action. pywinauto is the current adapter.
3. **Fallback/manual hand-off:** if the tree does not expose a reliable contact header, composer value, or action state, stop and ask the user to focus/select the correct conversation. Do not infer a match from a name appearing somewhere in the page.
4. **OCR or coordinates are last-resort only:** do not use them to authorize sending, calling, or ending a call without a fresh user-visible review and an unambiguous target. A screenshot can be stale and coordinates can land on another control after reflow.

### Message scenario matrix

| Scenario | Required behavior |
| --- | --- |
| Valid contact and text | Preflight → verify session → search → uniquely verify selected conversation → fill composer → read back draft → explicit approval → one send attempt → verify UI history and cleared composer |
| Same display name appears multiple times | Stop; request a more specific contact identifier or user selection. Never pick the first match silently |
| Search result missing or no session | Stop before composing; report sign-in/search issue, do not type into a guessed field |
| Draft cannot be read back | Do not send. Leave the state for the user to inspect or clear manually |
| Click/bridge timeout during Send | Treat outcome as **unknown**. Never click Send again automatically; inspect history first and ask the user if still ambiguous |
| Message appears in history but composer remains populated | Report unconfirmed; do not infer success or retry |
| Exact text already existed in history before send | Capture/compare pre-send state where available; if the UI cannot distinguish the new message, report unknown rather than success |
| Empty/oversized message or control characters in recipient | Reject before touching UI |
| App closed, locked desktop, UI tree unavailable | Fail closed; explain the prerequisite and offer browser mode/manual continuation |
| Platform lacks a configured call control | Reject before requesting approval; capabilities must be platform-specific |

### Call scenario matrix

- Require an explicit recipient and verify the active conversation before call-related clicks.
- Start voice/video call only if that platform has a configured control. Do not equate a button click with a connected call.
- End-call actions must locate one unique end/leave/disconnect control and verify the resulting UI. If call state is ambiguous, hand off to the user.
- Never auto-answer, join, leave, mute, or turn on video as an inferred side effect of opening a conversation.
- A timeout after a call button is clicked is an unknown outcome, not permission to click again.

### Recovery and retry policy

- Retry a locator only when the adapter explicitly says **no target was found**, which means no action was performed.
- Do not retry after timeout, bridge disconnection, generic failure, focus loss, or any response that could mean an action already happened.
- For uncertain sends, switch to a read-only inspect/history check; never replay the same message blindly.
- Keep each operation single-purpose. Sending, starting a call, and ending a call each require separate approval and separate verification.
- Do not automate bulk messages, group membership changes, contact scraping, hidden/background content, or account security changes.

### Architecture and alternatives researched

- **Recommended now:** keep the existing Edge bridge for browser workflows and local Windows UI Automation for installed apps; add per-platform adapters and scenario tests rather than a universal set of label guesses.
- **Browser alternative:** Playwright-style role/label locators are more robust when the app is controlled through a supported browser context because actions wait for uniqueness, visibility, stability and enabled state. Integrate only if the existing extension bridge cannot expose those guarantees and the user's local authenticated profile can be preserved safely.
- **Windows alternative:** Microsoft's WinApp CLI provides inspect/search/get-value/invoke/set-value UI Automation operations and favors UIA patterns over input injection. It is a promising optional adapter for apps where pywinauto cannot reliably invoke or read controls; it should not be a required runtime until installation, licensing, versioning and local-only behavior are validated.
- **No API fallback:** if a UI is inaccessible or changes, stop and request manual help rather than switching to unofficial endpoints, cookies, private databases, or session-token extraction.

The controller's unit tests use mocks. These scenarios are policy and regression coverage, not proof that every listed social platform currently exposes compatible live UI controls.
