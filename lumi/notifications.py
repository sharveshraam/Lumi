"""Desktop notifications that work even when the window is closed.

Delivery order:

1. a native Windows toast (via ``winrt`` or PowerShell's WinRT bridge) when we
   are on Windows and the helper is available;
2. the Qt tray balloon, which is what the UI already shows;
3. a plain log line - never an exception.

Reminders must not be lost because a notification backend is missing, so every
failure here is swallowed and reported as ``False``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Optional

log = logging.getLogger("lumi.notifications")


def notify(title: str, message: str, *, sound: bool = True) -> bool:
    """Show a desktop notification.  Returns True when one was actually shown."""
    title = (title or "LUMI").strip()[:120]
    message = (message or "").strip()[:400]
    if not message:
        return False

    if sys.platform.startswith("win"):
        if _windows_toast(title, message):
            if sound:
                _beep()
            return True

    if sound:
        _beep()
    log.info("notification: %s - %s", title, message)
    return False


def _windows_toast(title: str, message: str) -> bool:
    """Best-effort native toast; falls back to a PowerShell balloon."""
    try:
        import winrt.windows.ui.notifications as notifications  # type: ignore
        import winrt.windows.data.xml.dom as dom  # type: ignore

        template = notifications.ToastNotificationManager.get_template_content(
            notifications.ToastTemplateType.TOAST_TEXT02
        )
        text_nodes = template.get_elements_by_tag_name("text")
        text_nodes[0].append_child(template.create_text_node(title))
        text_nodes[1].append_child(template.create_text_node(message))
        toast = notifications.ToastNotification(template)
        notifications.ToastNotificationManager.create_toast_notifier("LUMI").show(toast)
        return True
    except Exception:  # noqa: BLE001 - winrt is optional
        pass

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return False
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n = $t.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($t.CreateTextNode('{_ps_escape(title)}')) > $null;"
        f"$n.Item(1).AppendChild($t.CreateTextNode('{_ps_escape(message)}')) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('LUMI').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($t));"
    )
    try:
        subprocess.run(  # noqa: S603 - fixed command, escaped arguments
            [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("PowerShell toast failed: %s", exc)
        return False


def _ps_escape(text: str) -> str:
    return text.replace("'", "''").replace("\r", " ").replace("\n", " ")


def _beep() -> None:
    try:
        if sys.platform.startswith("win"):
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:  # noqa: BLE001 - sound is cosmetic
        pass
