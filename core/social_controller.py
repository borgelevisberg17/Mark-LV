"""Shared contract and coordinator for application-specific social controllers.

This module deliberately contains no app automation. Concrete controllers must be
registered explicitly and advertise only capabilities they can verify. Side-effecting
operations are routed through the existing core.execution confirmation and audit path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Mapping, Protocol, Sequence


class SocialAction(str, Enum):
    LIST_CONVERSATIONS = "list_conversations"
    READ_MESSAGES = "read_messages"
    INSPECT_VISIBLE_PAGE = "inspect_visible_page"
    DRAFT_MESSAGE = "draft_message"
    SEND_MESSAGE = "send_message"
    LIST_GROUPS = "list_groups"
    READ_GROUP_MEMBERS = "read_group_members"
    ADD_GROUP_MEMBER = "add_group_member"
    REMOVE_GROUP_MEMBER = "remove_group_member"
    START_CALL = "start_call"
    END_CALL = "end_call"
    SEND_ATTACHMENT = "send_attachment"


# Actions that can cause an externally visible change or disrupt a live session.
CONFIRMATION_REQUIRED = frozenset({
    SocialAction.SEND_MESSAGE,
    SocialAction.ADD_GROUP_MEMBER,
    SocialAction.REMOVE_GROUP_MEMBER,
    SocialAction.START_CALL,
    SocialAction.END_CALL,
    SocialAction.SEND_ATTACHMENT,
})


@dataclass(frozen=True)
class SocialCapability:
    """A capability a concrete controller can currently perform and verify."""

    action: SocialAction
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class SocialAppDescriptor:
    """Stable identity and honest capability declaration for one target app."""

    app_id: str
    display_name: str
    capabilities: tuple[SocialCapability, ...] = ()
    enabled: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)

    def supports(self, action: SocialAction) -> bool:
        return any(cap.action == action and cap.available for cap in self.capabilities)


@dataclass(frozen=True)
class SocialActionResult:
    """Structured outcome; success means the controller verified the requested effect."""

    success: bool
    message: str
    verified: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


class SocialController(Protocol):
    """Contract implemented by app-specific adapters; no generic keystroke promises."""

    @property
    def descriptor(self) -> SocialAppDescriptor: ...

    def execute(
        self, action: SocialAction, parameters: Mapping[str, Any]
    ) -> SocialActionResult: ...


class SocialControllerError(RuntimeError):
    """Base error for controller registration and dispatch."""


class UnsupportedSocialAction(SocialControllerError):
    """Raised when an app has not declared an action as available."""


class SocialControllerRegistry:
    """Thread-safe registry; adapters are opt-in and never inferred from app names."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._controllers: dict[str, SocialController] = {}

    def register(self, controller: SocialController) -> None:
        descriptor = controller.descriptor
        app_id = descriptor.app_id.strip().lower()
        if not app_id or app_id != descriptor.app_id:
            raise ValueError("app_id must be non-empty, lowercase, and canonical.")
        if not descriptor.display_name.strip():
            raise ValueError("display_name must not be empty.")
        with self._lock:
            if app_id in self._controllers:
                raise ValueError(f"Social controller already registered: {app_id}")
            self._controllers[app_id] = controller

    def unregister(self, app_id: str) -> bool:
        with self._lock:
            return self._controllers.pop(app_id.strip().lower(), None) is not None

    def get(self, app_id: str) -> SocialController | None:
        with self._lock:
            controller = self._controllers.get(app_id.strip().lower())
            if controller is None or not controller.descriptor.enabled:
                return None
            return controller

    def descriptors(self) -> tuple[SocialAppDescriptor, ...]:
        with self._lock:
            return tuple(
                controller.descriptor
                for controller in self._controllers.values()
                if controller.descriptor.enabled
            )

    def dispatch(
        self, app_id: str, action: SocialAction | str, parameters: Mapping[str, Any] | None = None
    ) -> SocialActionResult:
        controller = self.get(app_id)
        if controller is None:
            return SocialActionResult(False, f"Social app '{app_id}' is not registered or enabled.")
        try:
            normalized_action = action if isinstance(action, SocialAction) else SocialAction(action)
        except ValueError:
            return SocialActionResult(False, f"Unknown social action: {action}")
        if not controller.descriptor.supports(normalized_action):
            return SocialActionResult(
                False,
                f"{controller.descriptor.display_name} does not currently declare "
                f"'{normalized_action.value}' as supported.",
            )
        try:
            result = controller.execute(normalized_action, parameters or {})
        except Exception as exc:
            # Never reinterpret exceptions as success or automatically replay side effects.
            return SocialActionResult(
                False,
                f"{controller.descriptor.display_name} operation failed; verify the app state before retrying.",
                verified=False,
                details={"error_type": type(exc).__name__},
            )
        if not isinstance(result, SocialActionResult):
            return SocialActionResult(
                False, "Controller returned an invalid result; the operation was not verified."
            )
        if result.success and not result.verified:
            return SocialActionResult(
                False,
                "Controller reported success without verification; outcome is treated as unconfirmed.",
                verified=False,
            )
        return result


def request_social_operation(
    *,
    app_id: str,
    action: SocialAction | str,
    parameters: Mapping[str, Any],
    operation,
    player=None,
) -> str:
    """Run a side-effecting social action through the app's existing approval/audit path.

    The operation callback is invoked only after the user approves the existing
    confirmation dialog. Parameters are not copied into audit metadata.
    """
    from core.execution import request_execution

    normalized_action = action if isinstance(action, SocialAction) else SocialAction(action)
    if normalized_action not in CONFIRMATION_REQUIRED:
        return str(operation() or "Operation completed.")
    app = app_id.strip().lower()
    if not app:
        raise ValueError("app_id must not be empty.")
    return request_execution(
        key=f"social:{app}:{normalized_action.value}",
        title=f"{normalized_action.value.replace('_', ' ').title()} — {app}",
        detail=(
            f"Application: {app}\n"
            f"Action: {normalized_action.value.replace('_', ' ')}\n"
            "Approve only if the target and intended effect are correct."
        ),
        operation=operation,
        player=player,
        category="desktop",
        permissions=("social", normalized_action.value),
        fingerprint=_operation_fingerprint(app, normalized_action, parameters),
    )


def _operation_fingerprint(
    app_id: str, action: SocialAction, parameters: Mapping[str, Any]
) -> str:
    """Stable, privacy-conscious fingerprint used only for idempotency, not logging."""
    import hashlib
    import json

    # Include normalized parameters to prevent different recipients/messages from
    # sharing an idempotency key. Only the hash is passed to the operation journal.
    payload = json.dumps(
        {"app": app_id, "action": action.value, "parameters": parameters},
        sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


# Shared process registry. Adapters register during explicit initialization.
registry = SocialControllerRegistry()
