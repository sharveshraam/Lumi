"""Clipboard helpers - used only when the user's own content must be preserved.

LUMI occasionally needs the clipboard (for example, to read the text of a web
page when accessibility APIs are not available).  Whenever it uses the clipboard
it saves and restores the previous contents, so the user never loses what they
had copied.  If the clipboard cannot be read, the operation is skipped rather
than silently discarding the user's data.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator, Optional

log = logging.getLogger("lumi.automation.clipboard")

MAX_CLIPBOARD_BYTES = 4 * 1024 * 1024


def read_clipboard() -> Optional[str]:
    """Return the clipboard text, or ``None`` when it cannot be read."""
    try:
        return _read_tk()
    except Exception as exc:
        log.debug("tkinter clipboard read failed: %s", exc)

    if sys.platform.startswith("win"):
        try:
            return _read_windows()
        except Exception as exc:  # pragma: no cover - depends on host
            log.debug("windows clipboard read failed: %s", exc)
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5, check=False)
            return result.stdout
        except Exception as exc:  # pragma: no cover
            log.debug("pbpaste failed: %s", exc)
    else:
        for command in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
                if result.returncode == 0:
                    return result.stdout
            except Exception:
                continue
    return None


def write_clipboard(text: str) -> bool:
    """Replace the clipboard contents.  Returns success."""
    payload = text or ""
    try:
        _write_tk(payload)
        return True
    except Exception as exc:
        log.debug("tkinter clipboard write failed: %s", exc)

    if sys.platform.startswith("win"):
        try:
            _write_windows(payload)
            return True
        except Exception as exc:  # pragma: no cover
            log.debug("windows clipboard write failed: %s", exc)
    elif sys.platform == "darwin":
        try:
            subprocess.run(["pbcopy"], input=payload, text=True, timeout=5, check=False)
            return True
        except Exception:  # pragma: no cover
            return False
    else:
        for command in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            try:
                subprocess.run(command, input=payload, text=True, timeout=5, check=False)
                return True
            except Exception:
                continue
    return False


def clear_clipboard() -> None:
    write_clipboard("")


@contextmanager
def preserve_clipboard(*, restore: bool = True) -> Iterator[Optional[str]]:
    """Yield the current clipboard text, restoring it on exit when possible.

    If the previous contents cannot be read (binary data, for example) the
    caller is told with ``None`` and restoration is skipped, so a screenshot in
    the clipboard is never replaced by an empty string.
    """
    previous = read_clipboard()
    try:
        yield previous
    finally:
        if restore and previous is not None:
            write_clipboard(previous)
        elif restore and previous is None:
            log.debug("clipboard contents could not be read; leaving it untouched")


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


def _read_tk() -> Optional[str]:
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    finally:
        root.destroy()


def _write_tk(text: str) -> None:
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    finally:
        root.destroy()


def _read_windows() -> Optional[str]:  # pragma: no cover - Windows only
    import ctypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _write_windows(text: str) -> None:  # pragma: no cover - Windows only
    import ctypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    payload = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        raise OSError("could not open the clipboard")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise OSError("could not allocate clipboard memory")
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, payload, len(payload))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()
