# Mark-LV Existing Edge Session Bridge

This extension lets Mark-LV operate the active tab in the **already-open Edge profile**. It does not launch another Edge process or copy browser data.

## Install (Windows / Edge)

1. Update Mark-LV to a build containing `core/browser_bridge.py` and start it.
2. Open Edge and visit `edge://extensions`.
3. Enable **Developer mode**, choose **Load unpacked**, and select the `extensions/edge-bridge` folder from the Mark-LV repository.
4. Open the extension's **Details → Extension options**.
5. Open `%USERPROFILE%\.mark-lv\browser_bridge.token` in Notepad, copy the full token, and paste it into the extension options. Save.
6. Return to a normal website tab and keep Edge and Mark-LV running. The extension popup shows connection status.

## Security and limitations

- The local service binds only to `127.0.0.1`; requests require a random bearer token stored in the user's home directory.
- The extension has broad website access because automation must work on sites the user chooses. Review its source before enabling it.
- It can read and interact with ordinary HTTP/HTTPS pages in the active tab. Edge internal pages, extension pages, browser settings and the Web Store are protected by Edge and cannot be scripted.
- The bridge supports navigation, search, active-tab actions, page text/URL, click/type, scrolling, forms, back/forward/reload and closing the active tab.
- The extension does not bypass CAPTCHAs, MFA, website access controls or user confirmation prompts.
- If the extension is not connected, Mark-LV must not silently launch another instance using the real Edge profile.

## WhatsApp access in Mark-LV

The built-in `whatsapp` action uses this same extension and the active WhatsApp
Web session. It can open WhatsApp Web, report whether the chat list is loaded,
list visible chats, read recent visible messages from a named chat, and send a
text message after Mark-LV displays an approval prompt. Sending is reported as
successful only when the outgoing message appears in the open conversation.
For incoming-message alerts, ask Mark-LV to monitor the exact contact or group
name. It checks the chat-list preview about every 20 seconds, requires an
unread marker, and shows a desktop notification plus an in-app alert. Monitoring
continues while Mark-LV is running; ask it to stop monitoring when you no longer
want alerts. The monitor stores the selected chat name and a hash of the last
preview locally, not the message history.
Keep WhatsApp Web open in Edge and configure the bridge token above. This is
local browser UI access, not access to WhatsApp's private databases or a
WhatsApp API; the user must complete login and any device linking in WhatsApp.
The target chat must be present in the visible chat list, and alerts depend on
WhatsApp Web exposing its unread marker and preview to the extension.
Attachments, calls, contact management, and account settings are not supported
by this integration.
