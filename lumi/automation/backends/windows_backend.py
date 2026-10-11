"""Windows automation backend.

Strategy, in order of preference:

1. **UI Automation** (via ``pywinauto``) - elements have names and states, so a
   click can be verified by reading the control afterwards.
2. **pyautogui** - raw keyboard/mouse, used only when no accessible element
   exists, or for global actions (screenshot, scroll at a point, hotkeys).
3. Nothing - a :class:`NullDesktopBackend` is used and every tool reports the
   reason honestly.

Everything is imported lazily and guarded: a missing optional package never
breaks LUMI, it just disables the corresponding capability.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from lumi.automation.backends.base import (
    BackendUnavailable,
    DesktopBackend,
    InputBackend,
    Screenshot,
    UiElement,
    WindowInfo,
    find_best_window,
)
from lumi.utils.timeutil import now, to_iso

log = logging.getLogger("lumi.automation.windows")

CAPABILITIES = frozenset({"windows", "uia", "input", "screenshot", "clipboard"})


class WindowsDesktopBackend(DesktopBackend):
    """UI Automation through pywinauto."""

    name = "windows-uia"
    is_real = True
    capabilities = CAPABILITIES

    def __init__(self) -> None:
        self._desktop: Any = None
        self._pywinauto_error = ""

    # ------------------------------------------------------------- set-up
    def available(self) -> tuple[bool, str]:
        if not sys.platform.startswith("win"):
            return False, "UI Automation is a Windows feature."
        try:
            import pywinauto  # noqa: F401
        except Exception as exc:
            self._pywinauto_error = f"pywinauto is not installed ({type(exc).__name__})"
            return False, "Install 'pywinauto' (pip install pywinauto) to let LUMI read window controls."
        return True, ""

    def _get_desktop(self) -> Any:
        if self._desktop is not None:
            return self._desktop
        from pywinauto import Desktop

        self._desktop = Desktop(backend="uia")
        return self._desktop

    # ------------------------------------------------------------- windows
    def list_windows(self, *, include_hidden: bool = False) -> list[WindowInfo]:
        desktop = self._get_desktop()
        windows: list[WindowInfo] = []
        try:
            wrappers = desktop.windows(visible_only=not include_hidden, enabled_only=False)
        except Exception as exc:  # pragma: no cover - depends on host
            raise BackendUnavailable(f"Could not enumerate windows: {exc}") from exc
        for wrapper in wrappers:
            try:
                info = wrapper.element_info
                rect = info.rectangle
                windows.append(
                    WindowInfo(
                        handle=getattr(info, "handle", None),
                        title=(info.name or "").strip(),
                        pid=getattr(info, "process_id", None),
                        app=(getattr(info, "class_name", "") or ""),
                        bounds=(rect.left, rect.top, rect.width(), rect.height()),
                        is_active=bool(getattr(wrapper, "is_active", lambda: False)()),
                        is_minimized=bool(getattr(wrapper, "is_minimized", lambda: False)()),
                    )
                )
            except Exception:  # pragma: no cover - windows come and go
                continue
        return windows

    def active_window(self) -> Optional[WindowInfo]:
        try:
            from pywinauto import Desktop

            wrapper = Desktop(backend="uia").window(active_only=True)
            info = wrapper.element_info
            rect = info.rectangle
            return WindowInfo(
                handle=getattr(info, "handle", None),
                title=(info.name or "").strip(),
                pid=getattr(info, "process_id", None),
                app=(getattr(info, "class_name", "") or ""),
                bounds=(rect.left, rect.top, rect.width(), rect.height()),
                is_active=True,
            )
        except Exception:
            return None

    def _wrapper_for(self, window: WindowInfo) -> Any:
        desktop = self._get_desktop()
        if window.handle is not None:
            try:
                return desktop.window(handle=window.handle)
            except Exception:  # pragma: no cover
                pass
        if window.title:
            try:
                return desktop.window(title_re=f".*{window.title[:60]}.*")
            except Exception:  # pragma: no cover
                pass
        raise BackendUnavailable(f"Window '{window.title}' is no longer open.")

    def focus_window(self, needle: str) -> Optional[WindowInfo]:
        windows = self.list_windows(include_hidden=True)
        target = find_best_window(windows, needle)
        if target is None:
            return None
        wrapper = self._wrapper_for(target)
        try:
            if getattr(wrapper, "is_minimized", lambda: False)():
                wrapper.restore()
            wrapper.set_focus()
            time.sleep(0.15)
            refreshed = self.active_window()
            if refreshed is not None:
                return refreshed
            return target
        except Exception as exc:
            raise BackendUnavailable(f"Could not bring '{target.title}' to the front: {exc}") from exc

    def wait_for_window(self, needle: str, *, timeout: float = 15.0, interval: float = 0.5) -> Optional[WindowInfo]:
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            found = find_best_window(self.list_windows(include_hidden=True), needle)
            if found is not None:
                return found
            time.sleep(max(0.2, interval))
        return None

    def close_window(self, needle: str) -> bool:
        windows = self.list_windows(include_hidden=True)
        target = find_best_window(windows, needle)
        if target is None:
            return False
        wrapper = self._wrapper_for(target)
        try:
            wrapper.close()
            return True
        except Exception as exc:  # pragma: no cover
            raise BackendUnavailable(f"Could not close '{target.title}': {exc}") from exc

    # ------------------------------------------------------- accessibility
    def ui_elements(
        self,
        window: WindowInfo,
        *,
        max_items: int = 200,
        control_types: Sequence[str] | None = None,
    ) -> list[UiElement]:
        wrapper = self._wrapper_for(window)
        wanted = {item.lower() for item in control_types} if control_types else None
        elements: list[UiElement] = []
        try:
            descendants = wrapper.descendants()
        except Exception as exc:
            raise BackendUnavailable(f"Could not read controls in '{window.title}': {exc}") from exc
        for index, child in enumerate(descendants):
            if len(elements) >= max_items:
                break
            try:
                info = child.element_info
                control_type = str(getattr(info, "control_type", "") or "")
                if wanted and control_type.lower() not in wanted:
                    continue
                rect = info.rectangle
                value = ""
                try:
                    value = str(child.get_value() or "") if hasattr(child, "get_value") else ""
                except Exception:
                    value = ""
                elements.append(
                    UiElement(
                        name=(info.name or "").strip(),
                        control_type=control_type,
                        automation_id=str(getattr(info, "automation_id", "") or ""),
                        class_name=str(getattr(info, "class_name", "") or ""),
                        enabled=bool(getattr(info, "enabled", True)),
                        visible=bool(getattr(info, "visible", True)),
                        value=value,
                        bounds=(rect.left, rect.top, rect.width(), rect.height()),
                        index=index,
                        backend_id=child,
                    )
                )
            except Exception:  # pragma: no cover - dynamic UI
                continue
        return elements

    def window_text(self, window: WindowInfo, *, max_chars: int = 20000) -> str:
        """Collect visible text from a window (labels, text values, document text)."""
        try:
            elements = self.ui_elements(window, max_items=600)
        except BackendUnavailable:
            raise
        chunks: list[str] = []
        for element in elements:
            control = element.control_type.lower()
            if control in {"text", "edit", "document", "hyperlink", "listitem", "header", "button", "menuitem"}:
                text = element.name or element.value
                if text:
                    chunks.append(text.strip())
        joined = "\n".join(dict.fromkeys(chunk for chunk in chunks if chunk))
        return joined[:max_chars]

    def click_element(self, element: UiElement) -> bool:
        target = element.backend_id
        if target is None:
            raise BackendUnavailable("That control is no longer available.")
        # Prefer the accessibility invoke pattern; fall back to a real click.
        for method_name in ("invoke", "click_input", "select"):
            method = getattr(target, method_name, None)
            if method is None:
                continue
            try:
                if method_name == "click_input":
                    method(double=True if element.control_type.lower() == "listitem" else False)
                else:
                    method()
                return True
            except Exception as exc:  # pragma: no cover - varies per control
                log.debug("click via %s failed: %s", method_name, exc)
        raise BackendUnavailable(f"Could not activate the control '{element.label}'.")

    def set_element_text(self, element: UiElement, text: str) -> bool:
        target = element.backend_id
        if target is None:
            raise BackendUnavailable("That field is no longer available.")
        for method_name in ("set_edit_text", "type_keys"):
            method = getattr(target, method_name, None)
            if method is None:
                continue
            try:
                if method_name == "set_edit_text":
                    method(text, with_newlines=True)
                else:
                    method(text, with_spaces=True, with_tabs=True, with_newlines=True, set_foreground=True)
                return True
            except Exception as exc:  # pragma: no cover
                log.debug("set text via %s failed: %s", method_name, exc)
        raise BackendUnavailable(f"Could not type into '{element.label}'.")

    def get_element_value(self, element: UiElement) -> str:
        target = element.backend_id
        if target is None:
            return element.value
        try:
            if hasattr(target, "get_value"):
                return str(target.get_value() or "")
        except Exception:  # pragma: no cover
            pass
        return element.value

    def wait_for_element(
        self,
        window: WindowInfo,
        needle: str,
        *,
        timeout: float = 10.0,
        interval: float = 0.4,
        control_types: Sequence[str] | None = None,
    ) -> Optional[UiElement]:
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            try:
                for element in self.ui_elements(window, max_items=300, control_types=control_types):
                    if element.matches(needle):
                        return element
            except BackendUnavailable:
                return None
            time.sleep(max(0.2, interval))
        return None


class PyAutoGuiInput(InputBackend):
    """Raw input through pyautogui, with the corner failsafe enabled."""

    name = "pyautogui"
    is_real = True

    def __init__(self) -> None:
        self._gui: Any = None
        self._error = ""

    def available(self) -> tuple[bool, str]:
        try:
            import pyautogui  # noqa: F401
        except Exception as exc:
            self._error = f"pyautogui is not installed ({type(exc).__name__})"
            return False, "Install 'pyautogui' (pip install pyautogui) to let LUMI control the keyboard and mouse."
        if os.environ.get("DISPLAY") is None and not sys.platform.startswith("win") and sys.platform != "darwin":
            return False, "No graphical display is available in this session."
        return True, ""

    def _gui_module(self) -> Any:
        if self._gui is not None:
            return self._gui
        import pyautogui

        pyautogui.FAILSAFE = True  # moving the mouse to a corner aborts
        pyautogui.PAUSE = 0.05
        self._gui = pyautogui
        return pyautogui

    def screen_size(self) -> tuple[int, int]:
        size = self._gui_module().size()
        return int(size.width), int(size.height)

    def position(self) -> tuple[int, int]:
        point = self._gui_module().position()
        return int(point.x), int(point.y)

    def move_to(self, x: int, y: int, *, duration: float = 0.1) -> None:
        self._gui_module().moveTo(int(x), int(y), duration=max(0.0, duration))

    def click(self, x: int | None = None, y: int | None = None, *, button: str = "left", clicks: int = 1) -> None:
        gui = self._gui_module()
        if x is not None and y is not None:
            gui.click(int(x), int(y), clicks=max(1, clicks), button=button)
        else:
            gui.click(clicks=max(1, clicks), button=button)

    def scroll(self, amount: int, *, x: int | None = None, y: int | None = None) -> None:
        gui = self._gui_module()
        if x is not None and y is not None:
            gui.moveTo(int(x), int(y))
        gui.scroll(int(amount))

    def type_text(self, text: str, *, interval: float = 0.01) -> None:
        self._gui_module().write(text, interval=max(0.0, interval))

    def press(self, key: str, *, presses: int = 1, interval: float = 0.05) -> None:
        self._gui_module().press(key, presses=max(1, presses), interval=max(0.0, interval))

    def hotkey(self, *keys: str) -> None:
        if not keys:
            return
        self._gui_module().hotkey(*keys)

    def screenshot(self, path: Path, *, region: tuple[int, int, int, int] | None = None) -> Screenshot:
        gui = self._gui_module()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        image = gui.screenshot(region=region) if region else gui.screenshot()
        image.save(str(path))
        width, height = image.size
        return Screenshot(path=path, width=int(width), height=int(height), taken_at=to_iso(now()) or "")

    def set_failsafe(self, enabled: bool = True) -> None:
        if self._gui is None:
            try:
                self._gui = self._gui_module()
            except Exception:
                return
        try:
            self._gui.FAILSAFE = bool(enabled)
        except Exception:  # pragma: no cover
            pass


__all__ = ["WindowsDesktopBackend", "PyAutoGuiInput"]
