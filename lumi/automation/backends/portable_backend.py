"""Portable (macOS/Linux) automation backend.

Used for development on non-Windows machines and as a last-resort fallback on
Windows when pywinauto is missing but pyautogui works.

Window management is best effort: ``wmctrl`` on Linux, ``osascript`` on macOS.
Accessibility-tree reading is not available, so tools that need it report that
plainly instead of guessing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Any, Optional, Sequence

from lumi.automation.backends.base import (
    BackendUnavailable,
    DesktopBackend,
    UiElement,
    WindowInfo,
    find_best_window,
    InputBackend,
)

log = logging.getLogger("lumi.automation.portable")

CAPABILITIES = frozenset({"windows", "input", "screenshot"})


class PortableDesktopBackend(DesktopBackend):
    """List/focus windows using OS utilities; no accessibility tree."""

    name = "portable"
    is_real = True
    capabilities = CAPABILITIES

    def available(self) -> tuple[bool, str]:
        if sys.platform.startswith("win"):
            return True, ""
        if sys.platform == "darwin":
            return True, ""
        if shutil.which("wmctrl") or shutil.which("xdotool"):
            return True, ""
        return False, "Window control needs 'wmctrl' or 'xdotool' on Linux."

    # ------------------------------------------------------------- windows
    def list_windows(self, *, include_hidden: bool = False) -> list[WindowInfo]:
        del include_hidden
        if sys.platform == "darwin":
            return self._list_windows_macos()
        return self._list_windows_linux()

    def _list_windows_linux(self) -> list[WindowInfo]:
        if not shutil.which("wmctrl"):
            raise BackendUnavailable("Install 'wmctrl' to let LUMI see and focus windows on Linux.")
        try:
            output = subprocess.run(["wmctrl", "-lp"], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendUnavailable(f"wmctrl failed: {exc}") from exc
        windows: list[WindowInfo] = []
        for line in (output.stdout or "").splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            handle, _desktop, pid, _host, title = parts
            try:
                pid_value: Optional[int] = int(pid)
            except ValueError:
                pid_value = None
            windows.append(WindowInfo(handle=handle, title=title.strip(), pid=pid_value, app=title.strip().split()[-1] if title else ""))
        return windows

    def _list_windows_macos(self) -> list[WindowInfo]:
        script = 'tell application "System Events" to get name of every process whose background only is false'
        try:
            output = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendUnavailable(f"osascript failed: {exc}") from exc
        names = [name.strip() for name in (output.stdout or "").split(",") if name.strip()]
        return [WindowInfo(handle=name, title=name, app=name) for name in names]

    def focus_window(self, needle: str) -> Optional[WindowInfo]:
        windows = self.list_windows()
        target = find_best_window(windows, needle)
        if target is None:
            return None
        if sys.platform == "darwin":
            script = f'tell application "{target.app}" to activate'
            subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6, check=False)
        elif shutil.which("wmctrl"):
            subprocess.run(["wmctrl", "-ia", str(target.handle)], capture_output=True, text=True, timeout=5, check=False)
        else:
            raise BackendUnavailable("Focusing windows requires wmctrl on Linux.")
        target.is_active = True
        return target

    @staticmethod
    def ui_elements(window: WindowInfo, **_: Any) -> list[UiElement]:
        raise BackendUnavailable(
            f"Reading the controls of '{window.title}' needs Windows UI Automation.",
            hint="On Windows, install pywinauto. Elsewhere, LUMI uses screenshots and keyboard control instead.",
        )

    @staticmethod
    def window_text(window: WindowInfo, **_: Any) -> str:
        raise BackendUnavailable(f"Reading text from '{window.title}' needs Windows UI Automation.")


class PortableInputBackend(InputBackend):
    """pyautogui input for non-Windows systems."""

    name = "pyautogui-portable"
    is_real = True

    def __init__(self) -> None:
        from lumi.automation.backends.windows_backend import PyAutoGuiInput

        self._inner = PyAutoGuiInput()

    def available(self) -> tuple[bool, str]:
        return self._inner.available()

    def screen_size(self) -> tuple[int, int]:
        return self._inner.screen_size()

    def position(self) -> tuple[int, int]:
        return self._inner.position()

    def move_to(self, x: int, y: int, *, duration: float = 0.1) -> None:
        self._inner.move_to(x, y, duration=duration)

    def click(self, x: int | None = None, y: int | None = None, *, button: str = "left", clicks: int = 1) -> None:
        self._inner.click(x, y, button=button, clicks=clicks)

    def scroll(self, amount: int, *, x: int | None = None, y: int | None = None) -> None:
        self._inner.scroll(amount, x=x, y=y)

    def type_text(self, text: str, *, interval: float = 0.01) -> None:
        self._inner.type_text(text, interval=interval)

    def press(self, key: str, *, presses: int = 1, interval: float = 0.05) -> None:
        self._inner.press(key, presses=presses, interval=interval)

    def hotkey(self, *keys: str) -> None:
        self._inner.hotkey(*keys)

    def screenshot(self, path: Any, *, region: tuple[int, int, int, int] | None = None):
        return self._inner.screenshot(path, region=region)

    def set_failsafe(self, enabled: bool = True) -> None:
        self._inner.set_failsafe(enabled)


__all__ = ["PortableDesktopBackend", "PortableInputBackend"]
