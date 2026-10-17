"""UI-only adapter for inspecting social sites in the user's existing Edge session.

This module deliberately does not call social-network APIs, use cookies/tokens, or
create a browser profile. It talks to the existing authenticated Mark-LV Edge bridge,
which operates the user's visible, logged-in tab through the extension.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from core.social_controller import (
    SocialAction, SocialActionResult, SocialAppDescriptor, SocialCapability,
)

# Fixed allowlist: callers cannot supply arbitrary domains or make this adapter
# read a page outside the selected social service.
SOCIAL_HOSTS: dict[str, tuple[str, ...]] = {
    "whatsapp": ("web.whatsapp.com",),
    "telegram": ("web.telegram.org",),
    "discord": ("discord.com", "www.discord.com"),
    "messenger": ("messenger.com", "www.messenger.com"),
    "instagram": ("instagram.com", "www.instagram.com"),
    "teams": ("teams.microsoft.com",),
    "slack": ("app.slack.com",),
}


class BrowserUIDriver(Protocol):
    """Minimal UI bridge contract, injectable for tests."""

    def current_url(self) -> str: ...
    def visible_text(self) -> str: ...


class EdgeExtensionUIDriver:
    """Use only the existing local Edge-extension bridge; never fall back to APIs."""

    def _submit(self, action: str) -> str:
        # Import lazily so unit tests and non-Edge environments need no browser setup.
        from actions.browser_control import _try_existing_edge

        result = _try_existing_edge(action, "edge", {})
        if result is None:
            raise RuntimeError(
                "The existing Edge session is not connected to the Mark-LV bridge. "
                "Open Edge, enable the Mark-LV bridge extension, and retry."
            )
        value = str(result)
        if value.startswith((
            "The Edge extension did not respond",
            "Could not get page text:",
            "Bridge error:",
        )):
            raise RuntimeError("The local browser UI bridge could not verify the requested result.")
        return value

    def current_url(self) -> str:
        return self._submit("get_url").strip()

    def visible_text(self) -> str:
        return self._submit("get_text")


@dataclass
class BrowserSocialUIController:
    """Read visible page text only after verifying the active tab belongs to the app.

    This first UI adapter does not claim to identify a conversation, send messages,
    modify groups, or place calls. Those actions require app-specific selectors and
    independent verification and are intentionally not advertised here.
    """

    app_id: str
    display_name: str
    driver: BrowserUIDriver

    @property
    def descriptor(self) -> SocialAppDescriptor:
        if self.app_id not in SOCIAL_HOSTS:
            raise ValueError(f"Unsupported social app id: {self.app_id}")
        return SocialAppDescriptor(
            app_id=self.app_id,
            display_name=self.display_name,
            capabilities=(
                SocialCapability(
                    SocialAction.INSPECT_VISIBLE_PAGE,
                    True,
                    "Reads visible page text through the existing local browser UI; "
                    "does not interpret it as verified conversation/message data.",
                ),
            ),
            metadata={"transport": "local-browser-ui", "uses_social_api": "false"},
        )

    def execute(self, action: SocialAction, parameters) -> SocialActionResult:
        if action != SocialAction.INSPECT_VISIBLE_PAGE:
            return SocialActionResult(False, f"Unsupported UI action: {action.value}")
        url = self.driver.current_url()
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed_hosts = SOCIAL_HOSTS[self.app_id]
        if parsed.scheme != "https" or host not in allowed_hosts:
            return SocialActionResult(
                False,
                f"Active Edge tab is not an approved {self.display_name} page.",
                verified=False,
                details={"url": url[:500]},
            )
        text = self.driver.visible_text()
        if not text.strip():
            return SocialActionResult(
                False,
                "The approved social page returned no visible text.",
                verified=False,
                details={"url": url[:500]},
            )
        return SocialActionResult(
            True,
            f"Captured visible page text from {self.display_name}; conversation semantics are not inferred.",
            verified=True,
            details={"url": url[:500], "visible_text": text[:4000]},
        )
