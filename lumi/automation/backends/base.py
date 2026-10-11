"""Backend interfaces for desktop automation.

LUMI prefers *accessibility* interfaces (Windows UI Automation) over pixels:
elements have names, ids and states, and a click can be verified by re-reading
that state.  Coordinate-based input is only used when no accessible element
exists, and every such action is reported as "unverified".

The interfaces below are deliberately small.  A backend that cannot do
something says so (``BackendUnavailable`` / unsupported capability) instead of
pretending, which is what keeps the tool layer honest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


class BackendUnavailable(Exception):
    """Raised when a backend cannot perform an operation at all."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class WindowInfo:
    """A top-level window."""

    handle: Any = None
    title: str = ""
    pid: Optional[int] = None
    app: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, width, height
    is_active: bool = False
    is_minimized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": str(self.handle),
            "title": self.title,
            "pid": self.pid,
            "app": self.app,
            "bounds": list(self.bounds),
            "active": self.is_active,
            "minimized": self.is_minimized,
        }

    def matches(self, needle: str) -> bool:
        text = (needle or "").strip().lower()
        if not text:
            return False
        return text in self.title.lower() or text in self.app.lower()


@dataclass
class UiElement:
    """One accessible control inside a window."""

    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    enabled: bool = True
    visible: bool = True
    value: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    depth: int = 0
    index: int = 0
    backend_id: Any = None

    @property
    def label(self) -> str:
        return self.name or self.value or self.automation_id or self.control_type or "element"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.control_type,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "enabled": self.enabled,
            "visible": self.visible,
            "value": self.value[:200] if self.value else "",
            "bounds": list(self.bounds),
        }

    def matches(self, needle: str, *, exact: bool = False) -> bool:
        text = (needle or "").strip().lower()
        if not text:
            return False
        candidates = [self.name.lower(), self.automation_id.lower(), self.value.lower(), self.class_name.lower()]
        if exact:
            return text in candidates
        return any(text in candidate for candidate in candidates if candidate)


@dataclass
class Screenshot:
    path: Path
    width: int = 0
    height: int = 0
    taken_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "width": self.width, "height": self.height, "taken_at": self.taken_at}


class DesktopBackend(ABC):
    """Window and accessibility operations."""

    name: str = "base"
    is_real: bool = False
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """``(usable, reason)`` - e.g. missing dependency or wrong OS."""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # ------------------------------------------------------------- windows
    def list_windows(self, *, include_hidden: bool = False) -> list[WindowInfo]:
        raise BackendUnavailable(f"Listing windows is not supported by the '{self.name}' backend.")

    def active_window(self) -> Optional[WindowInfo]:
        raise BackendUnavailable(f"Reading the active window is not supported by the '{self.name}' backend.")

    def focus_window(self, needle: str) -> Optional[WindowInfo]:
        raise BackendUnavailable(f"Focusing windows is not supported by the '{self.name}' backend.")

    def wait_for_window(self, needle: str, *, timeout: float = 15.0, interval: float = 0.5) -> Optional[WindowInfo]:
        pass  # implemented by backends that can list windows

    def close_window(self, needle: str) -> bool:
        raise BackendUnavailable(f"Closing windows is not supported by the '{self.name}' backend.")

    # ----------------------------------------------------- accessibility
    def ui_elements(self, window: WindowInfo, *, max_items: int = 200, control_types: Sequence[str] | None = None) -> list[UiElement]:
        raise BackendUnavailable(f"Reading UI controls is not supported by the '{self.name}' backend.")

    def window_text(self, window: WindowInfo, *, max_chars: int = 20000) -> str:
        raise BackendUnavailable(f"Reading window text is not supported by the '{self.name}' backend.")

    def click_element(self, element: UiElement) -> bool:
        raise BackendUnavailable(f"Clicking UI controls is not supported by the '{self.name}' backend.")

    def set_element_text(self, element: UiElement, text: str) -> bool:
        raise BackendUnavailable(f"Typing into UI controls is not supported by the '{self.name}' backend.")

    def get_element_value(self, element: UiElement) -> str:
        return element.value


class InputBackend(ABC):
    """Raw keyboard, mouse and screen operations."""

    name: str = "base"
    is_real: bool = False

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        ...

    def screen_size(self) -> tuple[int, int]:
        raise BackendUnavailable(f"Screen size is not available from the '{self.name}' backend.")

    def position(self) -> tuple[int, int]:
        raise BackendUnavailable(f"Mouse position is not available from the '{self.name}' backend.")

    def move_to(self, x: int, y: int, *, duration: float = 0.1) -> None:
        raise BackendUnavailable(f"Moving the mouse is not supported by the '{self.name}' backend.")

    def click(self, x: int | None = None, y: int | None = None, *, button: str = "left", clicks: int = 1) -> None:
        raise BackendUnavailable(f"Clicking is not supported by the '{self.name}' backend.")

    def scroll(self, amount: int, *, x: int | None = None, y: int | None = None) -> None:
        raise BackendUnavailable(f"Scrolling is not supported by the '{self.name}' backend.")

    def type_text(self, text: str, *, interval: float = 0.01) -> None:
        raise BackendUnavailable(f"Typing is not supported by the '{self.name}' backend.")

    def press(self, key: str, *, presses: int = 1, interval: float = 0.05) -> None:
        raise BackendUnavailable(f"Key presses are not supported by the '{self.name}' backend.")

    def hotkey(self, *keys: str) -> None:
        raise BackendUnavailable(f"Hotkeys are not supported by the '{self.name}' backend.")

    def screenshot(self, path: Path, *, region: tuple[int, int, int, int] | None = None) -> Screenshot:
        raise BackendUnavailable(f"Screenshots are not supported by the '{self.name}' backend.")

    def set_failsafe(self, enabled: bool = True) -> None:
        """Enable the "slam the mouse into a corner to abort" safety net."""
        return None


class NullDesktopBackend(DesktopBackend):
    """Used when no automation library is installed.

    It reports *why* it cannot act, so the tool layer can explain the limitation
    to the user instead of failing silently.
    """

    name = "none"
    is_real = False
    capabilities: frozenset[str] = frozenset()

    def __init__(self, reason: str = "Desktop automation is not available on this system.") -> None:
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason


class NullInputBackend(InputBackend):
    name = "none"
    is_real = False

    def __init__(self, reason: str = "Keyboard and mouse input is not available on this system.") -> None:
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason


def describe_windows(windows: Iterable[WindowInfo], *, limit: int = 20) -> str:
    lines = []
    for window in list(windows)[:limit]:
        marker = " (active)" if window.is_active else ""
        lines.append(f"- {window.title or window.app or 'untitled'}{marker}")
    return "\n".join(lines) if lines else "No open windows were found."


def find_best_window(windows: Sequence[WindowInfo], needle: str) -> Optional[WindowInfo]:
    """Pick the most likely window for a search term (active window wins ties)."""
    candidates = [window for window in windows if window.matches(needle)]
    if not candidates:
        return None
    candidates.sort(key=lambda window: (not window.is_active, len(window.title)))
    return candidates[0]


@dataclass
class StepLog:
    """Record of one automation step (shown in the tool activity panel)."""

    action: str
    detail: str = ""
    ok: bool = True
    verified: Optional[bool] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "detail": self.detail,
            "ok": self.ok,
            "verified": self.verified,
            **self.extra,
        }

    def human(self) -> str:
        state = "ok" if self.ok else "failed"
        verify = "" if self.verified is None else (" (verified)" if self.verified else " (unverified)")
        return f"{self.action}: {self.detail} [{state}{verify}]"
