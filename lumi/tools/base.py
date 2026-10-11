"""Tool primitives.

A *tool* is the only way the assistant can affect the world.  Every tool
declares what it needs, what it costs and how risky it is.  The model may
*propose* a tool call; only :mod:`lumi.tools.executor` may run one, and only
after schema validation and a permission decision.

Three ideas keep this safe and testable:

``RiskLevel``
    Low risk actions the user has allowed run directly.  Consequential actions
    always stop and ask, right before execution.

``ToolContext``
    Everything a tool needs is passed in explicitly (settings, database,
    cancellation flag, confirmation callback).  No globals, so tests can build a
    context with mocks and a temporary directory.

``ToolResult``
    A tool never raises to the model.  Failures come back as a result with
    ``ok=False`` plus a human explanation, so the assistant can report the
    failure instead of pretending it succeeded.
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from lumi.version import APP_NAME


class RiskLevel(str, Enum):
    """How much damage a tool can do if it goes wrong."""

    SAFE = "safe"                    # read-only, local, instant
    MODERATE = "moderate"            # changes the user's data or opens apps
    CONSEQUENTIAL = "consequential"  # sends, submits, deletes, installs, spends

    @property
    def label(self) -> str:
        return {
            RiskLevel.SAFE: "Safe",
            RiskLevel.MODERATE: "Changes your data",
            RiskLevel.CONSEQUENTIAL: "Needs your approval",
        }[self]


class ToolError(Exception):
    """Raised inside a tool for a predictable, reportable failure."""

    def __init__(self, message: str, *, hint: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.retryable = retryable


class ToolCancelled(ToolError):
    """Raised when the user pressed stop while a tool was running."""

    def __init__(self, message: str = "Stopped at your request.") -> None:
        super().__init__(message)


@dataclass
class ToolResult:
    """Outcome of a tool call, safe to show to the user *and* to the model."""

    ok: bool
    summary: str
    data: Any = None
    error: str = ""
    hint: str = ""
    tool: str = ""
    duration_ms: int = 0
    truncated: bool = False
    verified: Optional[bool] = None
    """``True``/``False`` when the tool checked its own effect; ``None`` if it
    cannot be verified automatically."""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, summary: str, *, data: Any = None, verified: Optional[bool] = None, **extra: Any) -> "ToolResult":
        return cls(ok=True, summary=summary, data=data, verified=verified, extra=extra)

    @classmethod
    def failure(cls, summary: str, *, error: str = "", hint: str = "", data: Any = None) -> "ToolResult":
        return cls(ok=False, summary=summary, error=error or summary, hint=hint, data=data)

    @classmethod
    def cancelled(cls, summary: str = "Stopped at your request.") -> "ToolResult":
        return cls(ok=False, summary=summary, error=summary, extra={"cancelled": True})

    def to_payload(self, *, max_chars: int = 4000) -> str:
        """Compact JSON payload for the model."""
        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary}
        if self.data is not None:
            payload["data"] = self.data
        if not self.ok:
            payload["error"] = self.error
            if self.hint:
                payload["hint"] = self.hint
        if self.verified is not None:
            payload["verified"] = self.verified
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            return text[:max_chars] + "... [truncated]"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "error": self.error,
            "hint": self.hint,
            "tool": self.tool,
            "duration_ms": self.duration_ms,
            "verified": self.verified,
            "extra": self.extra,
        }


@dataclass
class ConfirmationRequest:
    """Everything the UI needs to ask the user before a risky action."""

    tool: str
    title: str
    details: str
    risk: RiskLevel = RiskLevel.CONSEQUENTIAL
    preview: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    allow_session_grant: bool = True


class ConfirmationProvider(Protocol):
    """Callable used by the executor to obtain explicit approval."""

    def __call__(self, request: ConfirmationRequest) -> bool | str:  # pragma: no cover - protocol
        ...


class StopToken:
    """Cooperative cancellation shared with long-running tools.

    Wraps a :class:`threading.Event` so tools can poll ``stop_token.cancelled``
    between steps and raise :class:`ToolCancelled`.
    """

    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event or threading.Event()

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def raise_if_cancelled(self, context: str = "") -> None:
        if self.cancelled:
            raise ToolCancelled(f"Stopped at your request{' while ' + context if context else ''}.")


@dataclass
class ToolContext:
    """Dependencies handed to every tool call."""

    settings: Any
    paths: Any
    db: Any = None
    audit: Any = None
    stop_token: StopToken = field(default_factory=StopToken)
    confirm: Optional[ConfirmationProvider] = None
    notify: Optional[Callable[[str, str], None]] = None
    services: dict[str, Any] = field(default_factory=dict)
    session_id: str = "session"

    # -------------------------------------------------------------- helpers
    def service(self, name: str) -> Any:
        """Fetch a registered service (``tasks``, ``notes``, ``memory``, ...)."""
        if name not in self.services:
            raise ToolError(
                f"The '{name}' service is not available right now.",
                hint="This is usually a startup problem - restart LUMI and try again.",
            )
        return self.services[name]

    def has_service(self, name: str) -> bool:
        return name in self.services

    def check_cancelled(self, context: str = "") -> None:
        self.stop_token.raise_if_cancelled(context)

    def request_confirmation(self, request: ConfirmationRequest) -> bool:
        if self.confirm is None:
            return False
        try:
            outcome = self.confirm(request)
        except Exception:  # pragma: no cover - UI failure must not run the action
            return False
        if isinstance(outcome, str):
            return outcome.lower() in {"yes", "y", "true", "allow", "approve", "ok"}
        return bool(outcome)

    def notify_user(self, title: str, message: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(title, message)
        except Exception:  # pragma: no cover - notifications are best effort
            pass


class Tool(ABC):
    """Base class for every registered tool."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Give every tool a predictable, *strict* argument schema.

        ``additionalProperties`` defaults to ``False``: an unexpected argument is
        a sign the model misunderstood the tool, and it must be rejected rather
        than silently ignored.
        """
        super().__init_subclass__(**kwargs)
        declared = dict(getattr(cls, "parameters", None) or {})
        declared.setdefault("type", "object")
        declared.setdefault("properties", {})
        declared.setdefault("additionalProperties", False)
        cls.parameters = declared

    #: Unique snake_case identifier used by the model.
    name: str = ""
    #: One or two sentences: what it does, when to use it, what it needs.
    description: str = ""
    #: JSON schema for the arguments (draft 2020-12 subset, validated locally).
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    #: Grouping used by the UI ("tasks", "notes", "computer", ...).
    category: str = "general"
    risk: RiskLevel = RiskLevel.SAFE
    #: Settings field that must be truthy for this tool to be offered at all.
    requires_permission: str | None = None
    #: Hard cap on execution time.  ``None`` disables the watchdog.
    timeout_s: float | None = 20.0
    #: Always ask before running, regardless of risk level.
    always_confirm: bool = False
    #: Extra explanation shown in the confirmation dialog.
    confirmation_title: str = ""
    examples: tuple[str, ...] = ()

    # ------------------------------------------------------------ behaviour
    def is_available(self, context: ToolContext) -> tuple[bool, str]:
        """Return ``(available, reason)`` - e.g. missing optional dependency."""
        return True, ""

    def confirm_required(self, arguments: dict[str, Any], context: ToolContext) -> bool:
        """Per-argument confirmation logic (defaults to :attr:`always_confirm`)."""
        del arguments, context
        return self.always_confirm or self.risk is RiskLevel.CONSEQUENTIAL

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        """Build the dialog content shown before a consequential action."""
        del context
        title = self.confirmation_title or f"Allow {self.name}?"
        details = self.description.split("\n")[0]
        return ConfirmationRequest(
            tool=self.name,
            title=title,
            details=details,
            risk=self.risk,
            arguments=dict(arguments),
            preview=self.preview(arguments),
        )

    def preview(self, arguments: dict[str, Any]) -> str:
        """Human-readable preview of what will happen (used in dialogs)."""
        lines = [f"{key}: {value}" for key, value in arguments.items() if value not in (None, "", [], {})]
        return "\n".join(lines[:12])

    @abstractmethod
    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool.  Must not raise; return a failed result instead."""
        raise NotImplementedError

    # ---------------------------------------------------------- descriptors
    def doc_line(self) -> str:
        """Compact documentation injected into the system prompt."""
        params = self.parameters.get("properties") or {}
        required = set(self.parameters.get("required") or [])
        bits = []
        for name, spec in params.items():
            kind = spec.get("type", "any")
            if isinstance(kind, list):
                kind = "/".join(str(item) for item in kind)
            marker = "" if name in required else "?"
            description = (spec.get("description") or "").strip()
            bits.append(f"{name}{marker}:{kind}" + (f" ({description})" if description else ""))
        signature = ", ".join(bits) if bits else "no arguments"
        flags = []
        if self.risk is RiskLevel.CONSEQUENTIAL:
            flags.append("needs approval")
        if self.requires_permission:
            flags.append("optional")
        suffix = f" [{'; '.join(flags)}]" if flags else ""
        description = " ".join(self.description.split())
        return f"{self.name}({signature}) - {description}{suffix}"

    def to_schema(self) -> dict[str, Any]:
        """Ollama / OpenAI style function descriptor."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": " ".join(self.description.split()),
                "parameters": self.parameters,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk": self.risk.value,
            "parameters": self.parameters,
            "requires_permission": self.requires_permission,
            "timeout_s": self.timeout_s,
            "always_confirm": self.always_confirm,
            "examples": list(self.examples),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Tool {self.name} risk={self.risk.value}>"


def helper_unknown_argument(name: str, tool: Tool) -> ToolResult:
    """Uniform error for unknown/missing arguments."""
    allowed = ", ".join((tool.parameters.get("properties") or {}).keys()) or "none"
    return ToolResult.failure(
        f"{APP_NAME} could not run {tool.name}: unexpected argument '{name}'.",
        error=f"unknown argument: {name}",
        hint=f"Allowed arguments: {allowed}.",
    )
