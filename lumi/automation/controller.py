"""Automation controller: the only place that touches the user's desktop.

Everything a computer-control tool needs goes through here, which gives LUMI one
place to enforce:

* the permission switches in Settings,
* the step budget and inter-step delays,
* cooperative cancellation (Stop button and the emergency-stop hotkey),
* verification - "did that click actually do anything?",
* a human-readable step log that the tool activity panel shows.

When a backend is missing (no pywinauto, no display) every method raises
:class:`AutomationUnavailable` with an explanation instead of failing obscurely.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from lumi.automation import apps as app_catalogue
from lumi.automation.backends.base import (
    BackendUnavailable,
    DesktopBackend,
    InputBackend,
    NullDesktopBackend,
    NullInputBackend,
    Screenshot,
    StepLog,
    UiElement,
    WindowInfo,
    describe_windows,
    find_best_window,
)
from lumi.utils.timeutil import now, to_iso

log = logging.getLogger("lumi.automation.controller")


class AutomationUnavailable(Exception):
    """The requested capability is not available on this machine."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class AutomationStopped(Exception):
    """The user stopped automation (Stop button or emergency-stop hotkey)."""

    def __init__(self, message: str = "Automation stopped at your request.") -> None:
        super().__init__(message)
        self.message = message


@dataclass
class AutomationResult:
    """Outcome of an automation operation."""

    ok: bool
    action: str
    detail: str = ""
    verified: Optional[bool] = None
    error: str = ""
    hint: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    steps: list[StepLog] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "detail": self.detail,
            "verified": self.verified,
            "error": self.error,
            "hint": self.hint,
            "data": self.data,
            "steps": [step.to_dict() for step in self.steps],
            "duration_ms": self.duration_ms,
        }

    def step_lines(self) -> list[str]:
        return [step.human() for step in self.steps]

    @classmethod
    def failure(cls, action: str, message: str, *, hint: str = "", steps: Optional[list[StepLog]] = None) -> "AutomationResult":
        return cls(ok=False, action=action, error=message, hint=hint, steps=steps or [])


def _build_backends() -> tuple[DesktopBackend, InputBackend]:
    """Pick the best available backend pair for this machine."""
    from lumi.automation.backends.windows_backend import PyAutoGuiInput

    desktop: DesktopBackend
    if sys.platform.startswith("win"):
        from lumi.automation.backends.windows_backend import WindowsDesktopBackend

        candidate = WindowsDesktopBackend()
        ok, reason = candidate.available()
        desktop = candidate if ok else NullDesktopBackend(reason)
    else:
        from lumi.automation.backends.portable_backend import PortableDesktopBackend

        candidate = PortableDesktopBackend()
        ok, reason = candidate.available()
        desktop = candidate if ok else NullDesktopBackend(reason)

    raw_input = PyAutoGuiInput()
    ok, reason = raw_input.available()
    input_backend: InputBackend = raw_input if ok else NullInputBackend(reason)
    return desktop, input_backend


class AutomationController:
    """High-level, guarded desktop control."""

    def __init__(
        self,
        settings: Any,
        *,
        paths: Any = None,
        audit: Any = None,
        on_step: Callable[[StepLog], None] | None = None,
        external_stop_event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.audit = audit
        self._on_step = on_step
        self._stop_event = external_stop_event or threading.Event()
        self._desktop, self._input = _build_backends()
        self._steps_used = 0
        self._lock = threading.RLock()
        self._last_action: Optional[str] = None
        self._active = False
        self._screenshot_dir = (paths.cache_dir / "screenshots") if paths is not None else Path.cwd() / "screenshots"

    # ------------------------------------------------------------ properties
    @property
    def automation_settings(self) -> Any:
        return getattr(self.settings, "automation", None)

    @property
    def desktop(self) -> DesktopBackend:
        return self._desktop

    @property
    def input(self) -> InputBackend:
        return self._input

    @property
    def is_active(self) -> bool:
        """True while a tool is driving the computer (the UI shows an indicator)."""
        return self._active

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def set_external_stop_event(self, event: threading.Event) -> None:
        self._stop_event = event

    def refresh_backends(self) -> tuple[bool, str]:
        """Re-probe the backends (used after the user installs a library)."""
        self._desktop, self._input = _build_backends()
        desktop_ok, desktop_reason = self._desktop.available()
        input_ok, input_reason = self._input.available()
        if desktop_ok or input_ok:
            return True, "Ready"
        return False, desktop_reason or input_reason

    def status(self) -> dict[str, Any]:
        desktop_ok, desktop_reason = self._desktop.available()
        input_ok, input_reason = self._input.available()
        automation = self.automation_settings
        return {
            "backend": self._desktop.name,
            "desktop_ready": desktop_ok,
            "input_backend": self._input.name,
            "input_ready": input_ok,
            "reason": desktop_reason or input_reason,
            "enabled": bool(getattr(automation, "enabled", False)),
            "keyboard_mouse_allowed": bool(getattr(automation, "allow_keyboard_mouse", False)),
            "screenshots_allowed": bool(getattr(automation, "allow_screenshots", False)),
            "active": self._active,
            "stopped": self.stopped,
            "steps_used": self._steps_used,
            "platform": sys.platform,
        }

    # --------------------------------------------------------------- control
    def begin_sequence(self) -> None:
        """Reset the step budget at the start of a tool call."""
        with self._lock:
            self._steps_used = 0
            self._active = True
            self._stop_event.clear()

    def end_sequence(self) -> None:
        with self._lock:
            self._active = False

    def request_stop(self) -> None:
        """Called by the Stop button and the global emergency-stop hotkey."""
        self._stop_event.set()
        log.info("automation stop requested")
        self._record_step(StepLog(action="stop", detail="Stop requested by the user", ok=True))

    def clear_stop(self) -> None:
        self._stop_event.clear()

    def wait(self, seconds: float) -> None:
        """Sleep in small slices so a stop request is noticed quickly."""
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._guard("waiting")
            time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))

    # ---------------------------------------------------------------- guards
    def _guard(self, action: str) -> None:
        automation = self.automation_settings
        if automation is not None and not getattr(automation, "enabled", True):
            raise AutomationUnavailable(
                "Automation is switched off.", hint="Turn it on in Settings → Permissions."
            )
        if self._stop_event.is_set():
            raise AutomationStopped()
        budget = int(getattr(automation, "max_steps_per_sequence", 60) or 60)
        if self._steps_used >= budget:
            raise AutomationUnavailable(
                f"This sequence reached its limit of {budget} steps and stopped for safety.",
                hint="Ask for a smaller piece of the task, or raise the step limit in Settings → Permissions.",
            )

    def _require_input(self, action: str) -> None:
        self._guard(action)
        automation = self.automation_settings
        if automation is not None and not getattr(automation, "allow_keyboard_mouse", False):
            raise AutomationUnavailable(
                "Keyboard and mouse control is switched off.",
                hint="Enable 'Let LUMI control the keyboard and mouse' in Settings → Permissions.",
            )
        ok, reason = self._input.available()
        if not ok:
            raise AutomationUnavailable(reason or "Keyboard and mouse control is unavailable.")

    def _require_screenshots(self) -> None:
        automation = self.automation_settings
        if automation is not None and not getattr(automation, "allow_screenshots", False):
            raise AutomationUnavailable(
                "Screenshots are switched off.",
                hint="Enable 'Let LUMI take screenshots' in Settings → Permissions.",
            )

    def _record_step(self, step: StepLog) -> None:
        if not step.ok:
            log.info("step failed: %s", step.human())
        else:
            log.debug("step: %s", step.human())
        if self._on_step is not None:
            try:
                self._on_step(step)
            except Exception:  # pragma: no cover
                log.debug("step listener failed", exc_info=True)

    # -------------------------------------------------------------- launching
    def open_application(
        self,
        name: str,
        *,
        arguments: str = "",
        wait: bool = True,
        timeout: float = 20.0,
        expected_window: str = "",
    ) -> AutomationResult:
        """Launch a known application and, when possible, confirm it opened."""
        started = time.perf_counter()
        steps: list[StepLog] = []
        automation = self.automation_settings
        self._guard("opening an application")
        if automation is not None and not getattr(automation, "allow_app_launch", True):
            raise AutomationUnavailable(
                "Launching applications is switched off.",
                hint="Enable 'Let LUMI open applications' in Settings → Permissions.",
            )

        allowed_paths: list[Path] = []
        if self.paths is not None:
            allowed_paths.append(getattr(self.paths, "home", Path.cwd()))
        resolution = app_catalogue.resolve(name, allowed_paths=allowed_paths)
        if not resolution.resolved:
            message = resolution.notes[0] if resolution.notes else f"Could not find '{name}'."
            hint = ""
            if resolution.spec is None:
                options = ", ".join(spec.display_name for spec in app_catalogue.suggestions(name, limit=6))
                hint = f"Known applications include: {options}." if options else ""
            return AutomationResult.failure("open_application", message, hint=hint, steps=steps)

        target = resolution.target
        display = resolution.spec.display_name if resolution.spec else Path(target).name
        self._steps_used += 1
        try:
            self._launch(target, resolution.method, arguments)
        except Exception as exc:
            steps.append(StepLog(action="launch", detail=f"{display} via {resolution.method}", ok=False))
            return AutomationResult.failure(
                "open_application",
                f"Could not start {display}: {exc}",
                hint="Check that the application is installed and try again.",
                steps=steps,
            )
        steps.append(StepLog(action="launch", detail=f"{display} ({resolution.method})", ok=True))

        self.wait(float(getattr(automation, "action_delay_ms", 60)) / 1000 + 0.6)

        verified: Optional[bool] = None
        window_title = expected_window or (resolution.spec.display_name if resolution.spec else Path(target).stem)
        if wait and window_title:
            try:
                window = self._desktop.wait_for_window(window_title, timeout=min(timeout, 20.0))
            except BackendUnavailable:
                window = None
            if window is not None:
                verified = True
                steps.append(StepLog(action="verify", detail=f"window '{window.title}' is open", ok=True, verified=True))
            else:
                verified = False
                steps.append(
                    StepLog(action="verify", detail="window not detected yet", ok=True, verified=False)
                )

        self._record_step(steps[-1])
        detail = f"Opened {display}."
        if verified is False:
            detail += " I could not confirm its window appeared yet."
        return AutomationResult(
            ok=True,
            action="open_application",
            detail=detail,
            verified=verified,
            data=resolution.to_dict(),
            steps=steps,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def open_path(self, target: str, *, expect_window: str = "") -> AutomationResult:
        """Open a file, folder or URL with the operating system's default handler."""
        started = time.perf_counter()
        steps: list[StepLog] = []
        self._guard("opening a path")
        text = (target or "").strip().strip('"')
        if not text:
            return AutomationResult.failure("open_path", "No path was given.", steps=steps)

        is_url = text.lower().startswith(("http://", "https://", "mailto:"))
        path = Path(os.path.expandvars(os.path.expanduser(text)))
        if not is_url and not path.exists():
            return AutomationResult.failure(
                "open_path", f"'{text}' does not exist.", hint="Check the spelling, or search for the file first.", steps=steps
            )

        try:
            if is_url:
                self._open_url(text)
                steps.append(StepLog(action="open", detail=text, ok=True))
                kind = "link"
            elif sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
                steps.append(StepLog(action="open", detail=str(path), ok=True))
                kind = "folder" if path.is_dir() else "file"
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
                steps.append(StepLog(action="open", detail=str(path), ok=True))
                kind = "folder" if path.is_dir() else "file"
            else:
                subprocess.Popen(["xdg-open", str(path)])
                steps.append(StepLog(action="open", detail=str(path), ok=True))
                kind = "folder" if path.is_dir() else "file"
        except Exception as exc:
            return AutomationResult.failure("open_path", f"Could not open '{text}': {exc}", steps=steps)

        for step in steps:
            self._record_step(step)
        return AutomationResult(
            ok=True,
            action="open_path",
            detail=f"Opened {kind}: {text}",
            verified=None,
            data={"target": text, "kind": kind},
            steps=steps,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _open_url(self, url: str) -> None:
        import webbrowser

        if not webbrowser.open(url, new=2, autoraise=True):  # pragma: no cover - depends on host
            if sys.platform.startswith("win"):
                os.startfile(url)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["xdg-open", url])

    def _launch(self, target: str, method: str, arguments: str = "") -> None:
        args = arguments.split() if arguments else []
        if method == "uri":
            if sys.platform.startswith("win"):
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            return
        if sys.platform.startswith("win"):
            if method == "shortcut":
                os.startfile(target)  # type: ignore[attr-defined]
                return
            subprocess.Popen([target, *args], close_fds=True)
            return
        if sys.platform == "darwin" and target.endswith(".app"):
            subprocess.Popen(["open", target, *args])
            return
        subprocess.Popen([target, *args], close_fds=True)

    # ----------------------------------------------------------------- windows
    def list_windows(self, *, include_hidden: bool = False) -> AutomationResult:
        self._guard("listing windows")
        try:
            windows = self._desktop.list_windows(include_hidden=include_hidden)
        except BackendUnavailable as exc:
            return AutomationResult.failure("list_windows", exc.message, hint=exc.hint)
        except Exception as exc:  # pragma: no cover
            return AutomationResult.failure("list_windows", f"Could not list windows: {exc}")
        return AutomationResult(
            ok=True,
            action="list_windows",
            detail=describe_windows(windows, limit=25),
            data={"windows": [window.to_dict() for window in windows[:40]]},
            verified=True,
        )

    def focus_window(self, needle: str, *, timeout: float = 6.0) -> AutomationResult:
        steps: list[StepLog] = []
        self._guard("focusing a window")
        try:
            window = self._desktop.focus_window(needle)
            if window is None:
                window = self._desktop.wait_for_window(needle, timeout=timeout)
                if window is not None:
                    window = self._desktop.focus_window(window.title) or window
        except BackendUnavailable as exc:
            return AutomationResult.failure("focus_window", exc.message, hint=exc.hint, steps=steps)
        except AutomationStopped:
            raise
        except Exception as exc:
            return AutomationResult.failure("focus_window", f"Could not focus '{needle}': {exc}", steps=steps)

        self._steps_used += 1
        if window is None:
            steps.append(StepLog(action="focus", detail=f"no window matched '{needle}'", ok=False))
            return AutomationResult.failure(
                "focus_window",
                f"I could not find a window matching '{needle}'.",
                hint="Ask me to list the open windows first.",
                steps=steps,
            )

        # Verify by re-reading which window is active.
        verified: Optional[bool] = None
        try:
            active = self._desktop.active_window()
        except BackendUnavailable:
            active = None
        if active is not None:
            verified = active.matches(needle) or window.matches(active.title) or active.title == window.title
        steps.append(StepLog(action="focus", detail=window.title, ok=True, verified=verified))
        for step in steps:
            self._record_step(step)
        detail = f"Focused '{window.title}'."
        if verified is False:
            detail = f"Asked Windows to focus '{window.title}', but another window still has focus."
        return AutomationResult(
            ok=True,
            action="focus_window",
            detail=detail,
            verified=verified,
            data={"window": window.to_dict()},
            steps=steps,
        )

    def wait_for_window(self, needle: str, *, timeout: float = 15.0) -> AutomationResult:
        self._guard("waiting for a window")
        try:
            window = self._desktop.wait_for_window(needle, timeout=timeout)
        except BackendUnavailable as exc:
            return AutomationResult.failure("wait_for_window", exc.message, hint=exc.hint)
        if window is None:
            return AutomationResult.failure(
                "wait_for_window", f"No window matching '{needle}' appeared within {timeout:.0f} seconds."
            )
        return AutomationResult(ok=True, action="wait_for_window", detail=f"Found '{window.title}'.", verified=True, data={"window": window.to_dict()})

    # -------------------------------------------------------------- UI reading
    def read_ui_elements(
        self, needle: str = "", *, control_types: Sequence[str] | None = None, limit: int = 60
    ) -> AutomationResult:
        self._guard("reading controls")
        window, error = self._resolve_window(needle)
        if window is None:
            return AutomationResult.failure("read_ui_elements", error)
        try:
            elements = self._desktop.ui_elements(window, max_items=max(10, limit), control_types=control_types)
        except BackendUnavailable as exc:
            return AutomationResult.failure("read_ui_elements", exc.message, hint=exc.hint)
        summary_lines = [f"- {element.label} [{element.control_type}]" for element in elements[:40]]
        return AutomationResult(
            ok=True,
            action="read_ui_elements",
            detail=f"{len(elements)} controls in '{window.title}':\n" + "\n".join(summary_lines),
            verified=True,
            data={"window": window.to_dict(), "elements": [element.to_dict() for element in elements]},
        )

    def read_window_text(self, needle: str = "", *, max_chars: int = 12000) -> AutomationResult:
        self._guard("reading window text")
        window, error = self._resolve_window(needle)
        if window is None:
            return AutomationResult.failure("read_window_text", error)
        try:
            text = self._desktop.window_text(window, max_chars=max_chars)
        except BackendUnavailable as exc:
            return AutomationResult.failure("read_window_text", exc.message, hint=exc.hint)
        if not text.strip():
            return AutomationResult.failure(
                "read_window_text",
                f"'{window.title}' did not expose any readable text.",
                hint="Some apps (and some web pages) do not publish their text to accessibility tools.",
            )
        return AutomationResult(
            ok=True,
            action="read_window_text",
            detail=text,
            verified=True,
            data={"window": window.to_dict(), "text": text, "chars": len(text)},
        )

    def _resolve_window(self, needle: str) -> tuple[Optional[WindowInfo], str]:
        try:
            if not needle:
                window = self._desktop.active_window()
                if window is None:
                    return None, "I could not tell which window is active."
                return window, ""
            windows = self._desktop.list_windows()
            window = find_best_window(windows, needle)
            if window is None:
                return None, f"No open window matched '{needle}'."
            return window, ""
        except BackendUnavailable as exc:
            return None, exc.message

    # ----------------------------------------------------------------- input
    def type_text(self, text: str, *, secret: bool = False, interval_ms: int | None = None) -> AutomationResult:
        self._require_input("typing")
        if not text:
            return AutomationResult.failure("type_text", "There was nothing to type.")
        automation = self.automation_settings
        interval = (interval_ms if interval_ms is not None else int(getattr(automation, "typing_interval_ms", 12) or 12)) / 1000
        safe_text = "" if secret else text
        described = "(hidden text)" if secret else (text[:80] + ("…" if len(text) > 80 else ""))
        try:
            self._input.type_text(text, interval=max(0.0, interval))
        except Exception as exc:
            step = StepLog(action="type", detail=described, ok=False)
            self._record_step(step)
            return AutomationResult.failure("type_text", f"Could not type the text: {exc}", steps=[step])
        self._steps_used += 1
        step = StepLog(action="type", detail=described, ok=True, verified=None, extra={"secret": secret})
        self._record_step(step)
        self.wait(float(getattr(automation, "action_delay_ms", 60)) / 1000)
        return AutomationResult(
            ok=True,
            action="type_text",
            detail=f"Typed {len(text)} characters into the focused field.",
            verified=None,
            data={"characters": len(text), "secret": secret, "text_preview": safe_text[:200]},
            steps=[step],
        )

    def press_keys(self, keys: str, *, presses: int = 1) -> AutomationResult:
        self._require_input("pressing keys")
        sequence = [part.strip().lower() for part in str(keys).replace(" ", "").split("+") if part.strip()]
        if not sequence:
            return AutomationResult.failure("press_keys", "No keys were given.")
        try:
            if len(sequence) > 1:
                self._input.hotkey(*sequence)
            else:
                self._input.press(sequence[0], presses=max(1, int(presses)))
        except Exception as exc:
            step = StepLog(action="keys", detail="+".join(sequence), ok=False)
            self._record_step(step)
            return AutomationResult.failure("press_keys", f"Could not send '{keys}': {exc}", steps=[step])
        self._steps_used += 1
        step = StepLog(action="keys", detail="+".join(sequence), ok=True)
        self._record_step(step)
        return AutomationResult(ok=True, action="press_keys", detail=f"Pressed {'+'.join(sequence)}.", verified=None, steps=[step])

    def click_element(self, needle: str, *, window: str = "", exact: bool = False) -> AutomationResult:
        self._require_input("clicking a control")
        target_window, error = self._resolve_window(window)
        if target_window is None:
            return AutomationResult.failure("click_element", error)
        try:
            elements = self._desktop.ui_elements(target_window, max_items=400)
        except BackendUnavailable as exc:
            return AutomationResult.failure("click_element", exc.message, hint=exc.hint)
        match = next((element for element in elements if element.matches(needle, exact=exact)), None)
        if match is None:
            close = [element.label for element in elements if element.control_type.lower() in {"button", "menuitem", "tabitem"}][:12]
            return AutomationResult.failure(
                "click_element",
                f"I could not find a control named '{needle}' in '{target_window.title}'.",
                hint=("Visible buttons include: " + ", ".join(close)) if close else "",
            )
        if not match.enabled:
            return AutomationResult.failure("click_element", f"'{match.label}' is currently disabled.")
        try:
            self._desktop.click_element(match)
        except BackendUnavailable as exc:
            return AutomationResult.failure("click_element", exc.message, hint=exc.hint)
        except Exception as exc:
            return AutomationResult.failure("click_element", f"Clicking '{match.label}' failed: {exc}")

        self._steps_used += 1
        self.wait(float(getattr(self.automation_settings, "action_delay_ms", 60)) / 1000 + 0.25)

        # Verify: re-read the control and see whether it disappeared, got disabled
        # or changed value.  Absence of a change is not proof of failure, so the
        # result is reported as unverified rather than as a hard error.
        verified: Optional[bool] = None
        try:
            refreshed = self._desktop.ui_elements(target_window, max_items=400)
            still_there = next((element for element in refreshed if element.matches(needle, exact=exact)), None)
            if still_there is None:
                verified = True
            elif match.value and still_there.value and still_there.value != match.value:
                verified = True
            else:
                verified = None
        except Exception:  # pragma: no cover
            verified = None
        step = StepLog(action="click", detail=f"{match.label} in {target_window.title}", ok=True, verified=verified)
        self._record_step(step)
        return AutomationResult(
            ok=True,
            action="click_element",
            detail=f"Clicked '{match.label}'.",
            verified=verified,
            data={"element": match.to_dict(), "window": target_window.to_dict()},
            steps=[step],
        )

    def click_at(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> AutomationResult:
        """Coordinate click - only used when no accessible element exists."""
        self._require_input("clicking")
        try:
            self._input.click(int(x), int(y), button=button, clicks=clicks)
        except Exception as exc:
            return AutomationResult.failure("click_at", f"Click at ({x}, {y}) failed: {exc}")
        self._steps_used += 1
        step = StepLog(
            action="click_at", detail=f"({x}, {y})", ok=True, verified=None,
            extra={"note": "coordinate click - cannot be verified"},
        )
        self._record_step(step)
        return AutomationResult(
            ok=True,
            action="click_at",
            detail=f"Clicked at ({x}, {y}). This is a coordinate click, so I cannot verify what it hit.",
            verified=False,
            steps=[step],
        )

    def scroll(self, amount: int, *, x: int | None = None, y: int | None = None) -> AutomationResult:
        self._require_input("scrolling")
        try:
            self._input.scroll(int(amount), x=x, y=y)
        except Exception as exc:
            return AutomationResult.failure("scroll", f"Scrolling failed: {exc}")
        self._steps_used += 1
        return AutomationResult(ok=True, action="scroll", detail=f"Scrolled by {amount}.", verified=None)

    def set_text_in_element(self, needle: str, text: str, *, window: str = "", secret: bool = False) -> AutomationResult:
        """Type into a named field, verifying the value afterwards when possible."""
        self._require_input("filling a field")
        target_window, error = self._resolve_window(window)
        if target_window is None:
            return AutomationResult.failure("set_text_in_element", error)
        try:
            elements = self._desktop.ui_elements(target_window, max_items=400)
        except BackendUnavailable as exc:
            return AutomationResult.failure("set_text_in_element", exc.message, hint=exc.hint)
        match = next((element for element in elements if element.matches(needle)), None)
        if match is None:
            return AutomationResult.failure(
                "set_text_in_element", f"I could not find a field matching '{needle}' in '{target_window.title}'."
            )
        try:
            self._desktop.set_element_text(match, text)
        except BackendUnavailable:
            # Fall back to focusing with a click and typing.
            try:
                self._desktop.click_element(match)
                self._input.type_text(text, interval=max(0.0, int(getattr(self.automation_settings, "typing_interval_ms", 12)) / 1000))
            except Exception as exc:
                return AutomationResult.failure("set_text_in_element", f"Could not fill '{match.label}': {exc}")
        except Exception as exc:
            return AutomationResult.failure("set_text_in_element", f"Could not fill '{match.label}': {exc}")

        self._steps_used += 1
        self.wait(0.2)
        verified: Optional[bool] = None
        if not secret:
            try:
                refreshed = self._desktop.get_element_value(match)
                if refreshed:
                    verified = text.strip()[:40] in refreshed
            except Exception:  # pragma: no cover
                verified = None
        step = StepLog(action="fill", detail=f"{match.label} <- {'(hidden)' if secret else text[:60]}", ok=True, verified=verified)
        self._record_step(step)
        return AutomationResult(
            ok=True,
            action="set_text_in_element",
            detail=f"Filled '{match.label}'.",
            verified=verified,
            data={"element": match.to_dict(), "characters": len(text)},
            steps=[step],
        )

    # ------------------------------------------------------------ screenshots
    def screenshot(self, *, name: str = "", region: tuple[int, int, int, int] | None = None) -> AutomationResult:
        self._guard("taking a screenshot")
        self._require_screenshots()
        stamp = to_iso(now()) or ""
        filename = name or f"screenshot-{stamp.replace(':', '').replace('-', '')}.png"
        if not filename.lower().endswith(".png"):
            filename += ".png"
        target = self._screenshot_dir / filename
        try:
            shot = self._input.screenshot(target, region=region)
        except BackendUnavailable as exc:
            return AutomationResult.failure("screenshot", exc.message, hint=exc.hint)
        except Exception as exc:
            return AutomationResult.failure("screenshot", f"Could not capture the screen: {exc}")
        step = StepLog(action="screenshot", detail=str(shot.path), ok=True, verified=True)
        self._record_step(step)
        return AutomationResult(
            ok=True,
            action="screenshot",
            detail=f"Saved a screenshot to {shot.path} ({shot.width}×{shot.height}).",
            verified=True,
            data=shot.to_dict(),
            steps=[step],
        )

    # ------------------------------------------------------------------ misc
    def active_window(self) -> AutomationResult:
        try:
            window = self._desktop.active_window()
        except BackendUnavailable as exc:
            return AutomationResult.failure("active_window", exc.message, hint=exc.hint)
        if window is None:
            return AutomationResult.failure("active_window", "I could not read the active window.")
        return AutomationResult(ok=True, action="active_window", detail=window.title, verified=True, data={"window": window.to_dict()})

    def screen_size(self) -> tuple[int, int] | None:
        try:
            return self._input.screen_size()
        except Exception:
            return None

    def emergency_stop_hint(self) -> str:
        hotkey = getattr(self.automation_settings, "emergency_stop_hotkey", "Ctrl+Alt+Shift+S")
        return (
            f"Press {hotkey} or the Stop button to interrupt automation. "
            "Moving the mouse into a screen corner also aborts an automated sequence."
        )


def allowed_application_names() -> list[str]:
    return [spec.display_name for spec in app_catalogue.catalogue()]


def resolve_application(name: str, *, allowed_paths: Iterable[Path] = ()) -> app_catalogue.AppResolution:
    return app_catalogue.resolve(name, allowed_paths=allowed_paths)
