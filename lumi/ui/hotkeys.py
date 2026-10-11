"""A system-wide hotkey, so Stop works from whatever application has focus.

A ``QShortcut`` only fires while the window is active, which is useless for an
emergency stop: the moment you need it is the moment some other application has
the keyboard.  On Windows this registers a real hotkey with the OS
(``RegisterHotKey``) and picks up ``WM_HOTKEY`` through Qt's native event filter.

Everywhere else - and on Windows when registration is refused, because another
program already owns the combination - ``register()`` returns False with a
reason, and the caller falls back to the in-application shortcut.  Nothing here
ever raises: a hotkey that cannot be installed must not stop LUMI from starting.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

log = logging.getLogger("lumi.ui.hotkeys")

# Windows modifiers
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

#: Key names people write, mapped to Windows virtual-key codes.
_VK: dict[str, int] = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B,
    "ESCAPE": 0x1B, "SPACE": 0x20, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23,
    "HOME": 0x24, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "PRINT": 0x2C, "INSERT": 0x2D, "DELETE": 0x2E,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}
for _number in range(10):
    _VK[str(_number)] = 0x30 + _number
for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK[_letter] = ord(_letter)

#: ``Qt`` writes modifiers as "Ctrl+Alt+Shift+S"; Windows wants flags.
_MODIFIERS: dict[str, int] = {
    "CTRL": MOD_CONTROL,
    "CONTROL": MOD_CONTROL,
    "ALT": MOD_ALT,
    "SHIFT": MOD_SHIFT,
    "META": MOD_WIN,
    "WIN": MOD_WIN,
    "SUPER": MOD_WIN,
    "CMD": MOD_WIN,
}


class HotkeyError(ValueError):
    """The combination could not be understood."""


def parse_hotkey(text: str) -> tuple[int, int]:
    """Turn ``"Ctrl+Alt+Shift+S"`` into ``(modifier_flags, virtual_key)``.

    Raises :class:`HotkeyError` for anything it cannot map, so a typo in the
    settings is reported instead of silently doing nothing.
    """
    parts = [part.strip() for part in str(text or "").replace(" ", "").split("+") if part.strip()]
    if not parts:
        raise HotkeyError("A hotkey needs at least one key.")

    modifiers = 0
    key = ""
    for part in parts:
        upper = part.upper()
        if upper in _MODIFIERS:
            modifiers |= _MODIFIERS[upper]
            continue
        if key:
            raise HotkeyError(f"'{text}' has more than one key; use modifiers plus a single key.")
        key = upper

    if not key:
        raise HotkeyError(f"'{text}' is only modifiers; add a key such as F9 or S.")
    if key not in _VK:
        raise HotkeyError(f"'{key}' is not a key LUMI knows. Try a letter, a number or F1-F12.")
    if not modifiers:
        # A bare key would swallow that key everywhere.
        raise HotkeyError(f"'{text}' needs at least one modifier (Ctrl, Alt, Shift or Win).")
    return modifiers, _VK[key]


def is_supported(platform: str | None = None) -> bool:
    """True when a real system-wide hotkey can be registered here."""
    return (platform or sys.platform).startswith("win")


class GlobalHotkey:
    """A hotkey owned by the operating system, delivered through Qt's event loop."""

    def __init__(self, combination: str, callback: Callable[[], None]) -> None:
        self.combination = combination
        self.callback = callback
        self.reason = ""
        self._modifiers = 0
        self._key = 0
        self._id = 0
        self._filter: Optional[object] = None
        self._user32 = None

    # ------------------------------------------------------------------ setup
    def register(self) -> bool:
        """Install the hotkey.  Returns False (with ``reason``) if it cannot."""
        if not is_supported():
            self.reason = "system-wide hotkeys are a Windows feature; using the in-app shortcut instead"
            return False
        try:
            self._modifiers, self._key = parse_hotkey(self.combination)
        except HotkeyError as exc:
            self.reason = str(exc)
            return False

        try:
            import ctypes
            from ctypes import wintypes  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - Windows only
            self.reason = f"ctypes could not load the Windows API: {exc}"
            return False

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - Windows only
            self.reason = f"user32 is unavailable: {exc}"
            return False

        # A hotkey id is only unique within this thread, and 1 is free here.
        self._id = 1
        if not user32.RegisterHotKey(None, self._id, self._modifiers | MOD_NOREPEAT, self._key):
            error = ctypes.get_last_error()
            self.reason = (
                f"Windows refused {self.combination} (error {error}); "
                "another program probably uses it - pick a different combination in Settings"
            )
            return False

        self._user32 = user32
        self._install_filter()
        log.info("registered the global hotkey %s", self.combination)
        return True

    def _install_filter(self) -> None:
        """Ask Qt to hand Windows messages to us so WM_HOTKEY can be seen."""
        try:
            from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication
        except Exception as exc:  # pragma: no cover - Qt missing
            self.reason = f"Qt is not available: {exc}"
            self._unregister()
            return

        owner = self

        class _Filter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt naming
                try:
                    return owner._handle(event_type, message)
                except Exception:  # noqa: BLE001 - never break the event loop
                    log.debug("hotkey filter failed", exc_info=True)
                    return False, 0

        application = QCoreApplication.instance()
        if application is None:  # pragma: no cover - needs a running QApplication
            self.reason = "no Qt application is running yet"
            self._unregister()
            return
        self._filter = _Filter()  # keep a reference: Qt does not own it
        application.installNativeEventFilter(self._filter)

    def _handle(self, event_type, message):  # pragma: no cover - Windows only
        try:
            import ctypes
            from ctypes import wintypes  # type: ignore[attr-defined]

            msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY and msg.wParam == self._id:
            log.info("global hotkey %s pressed", self.combination)
            try:
                self.callback()
            except Exception:  # noqa: BLE001 - a broken callback must not kill the loop
                log.exception("the hotkey callback failed")
            return True, 0
        return False, 0

    # ---------------------------------------------------------------- cleanup
    def _unregister(self) -> None:
        if self._user32 is not None and self._id:
            try:
                self._user32.UnregisterHotKey(None, self._id)
            except Exception:  # pragma: no cover - Windows only
                log.debug("could not unregister the hotkey", exc_info=True)
        self._id = 0

    def unregister(self) -> None:
        """Give the combination back to the system."""
        self._unregister()
        if self._filter is not None:
            try:
                from PySide6.QtCore import QCoreApplication

                application = QCoreApplication.instance()
                if application is not None:
                    application.removeNativeEventFilter(self._filter)
            except Exception:  # pragma: no cover - Qt already gone
                log.debug("could not remove the hotkey filter", exc_info=True)
            self._filter = None

    def __enter__(self) -> "GlobalHotkey":
        self.register()
        return self

    def __exit__(self, *_exc) -> None:
        self.unregister()


__all__ = ["GlobalHotkey", "HotkeyError", "is_supported", "parse_hotkey"]
