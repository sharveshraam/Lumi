"""Computer-control tools.

Every one of these goes through :class:`lumi.automation.controller.AutomationController`,
which enforces the permission switches, the step budget, cancellation and
verification.  Tools prefer named controls over coordinates and always report
whether the effect could be verified.
"""

from __future__ import annotations

from typing import Any, Iterable

from lumi.automation.controller import AutomationResult, AutomationStopped, AutomationUnavailable
from lumi.tools.base import ConfirmationRequest, RiskLevel, Tool, ToolContext, ToolError, ToolResult
from lumi.tools.builtin._common import SERVICE_AUTOMATION, int_arg, ok, service, string_list


def _controller(context: ToolContext):
    try:
        return service(context, SERVICE_AUTOMATION)
    except ToolError:
        raise ToolError(
            "Desktop automation is not available in this session.",
            hint="Start LUMI on Windows with pyautogui (and pywinauto for accessibility) installed.",
        )


def _to_result(action: str, outcome: AutomationResult, *, data_extra: dict[str, Any] | None = None) -> ToolResult:
    """Convert an automation outcome into a tool result, honestly."""
    data = dict(outcome.data or {})
    if data_extra:
        data.update(data_extra)
    data["steps"] = outcome.step_lines()
    if outcome.ok:
        summary = outcome.detail or f"{action} finished."
        if outcome.verified is False:
            summary += " (I could not verify this step - please check the screen.)"
        return ToolResult.success(summary, data=data, verified=outcome.verified)
    return ToolResult.failure(outcome.error or f"{action} failed.", hint=outcome.hint, data=data)


class OpenApplication(Tool):
    name = "open_application"
    description = (
        "Open a program the user has installed (Notepad, Calculator, Chrome, VS Code, Word, "
        "File Explorer, ...). Only known applications in the user's list can be launched."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Application name, e.g. 'chrome', 'vscode', 'notepad'."},
            "arguments": {"type": "string", "description": "Optional arguments, e.g. a folder to open in VS Code.", "maxLength": 500},
            "expected_window": {"type": "string", "description": "Window title to wait for, if you know it."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60, "description": "How long to wait for the window (default 20)."},
        },
        "required": ["name"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_app_launch"
    timeout_s = 40.0
    examples = ("open notepad", "open vs code", "start chrome")

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.open_application(
                str(arguments.get("name", "")),
                arguments=str(arguments.get("arguments", "")),
                expected_window=str(arguments.get("expected_window", "")),
                timeout=float(int_arg(arguments, "wait_seconds", 20)),
            )
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("open_application", outcome)


class OpenPath(Tool):
    name = "open_path"
    description = "Open a file, folder or web link with the default program on this computer."
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "A file path, folder path or https:// link."},
        },
        "required": ["target"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_app_launch"
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.open_path(str(arguments.get("target", "")))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("open_path", outcome)


class ListOpenWindows(Tool):
    name = "list_open_windows"
    description = "List the windows that are currently open, so you know what the user is looking at."
    parameters = {
        "type": "object",
        "properties": {"include_hidden": {"type": "boolean"}},
    }
    category = "computer"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.list_windows(include_hidden=bool(arguments.get("include_hidden", False)))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        return _to_result("list_open_windows", outcome)


class FocusWindow(Tool):
    name = "focus_window"
    description = "Bring an open window to the front by part of its title (for example 'notepad' or 'chrome')."
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Part of the window title."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 30},
        },
        "required": ["title"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.focus_window(str(arguments.get("title", "")), timeout=float(int_arg(arguments, "wait_seconds", 6)))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("focus_window", outcome)


class ReadWindowText(Tool):
    name = "read_window_text"
    description = (
        "Read the visible text of an application window using accessibility APIs, so you can explain "
        "or summarise what is on screen. Does not read passwords or hidden fields."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Part of the window title. Defaults to the active window."},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 40000},
        },
    }
    category = "computer"
    risk = RiskLevel.SAFE
    timeout_s = 30.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.read_window_text(
                str(arguments.get("title", "")), max_chars=int_arg(arguments, "max_chars", 12000)
            )
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        return _to_result("read_window_text", outcome)


class ReadUiControls(Tool):
    name = "read_ui_controls"
    description = (
        "List the accessible controls (buttons, fields, menus) of a window. Use this to find the exact "
        "name of something before clicking it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Part of the window title. Defaults to the active window."},
            "control_types": {"type": "array", "items": {"type": "string"}, "maxItems": 8, "description": "e.g. ['Button'] or ['Edit']."},
            "limit": {"type": "integer", "minimum": 5, "maximum": 300},
        },
    }
    category = "computer"
    risk = RiskLevel.SAFE
    timeout_s = 30.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.read_ui_elements(
                str(arguments.get("title", "")),
                control_types=string_list(arguments.get("control_types")) or None,
                limit=int_arg(arguments, "limit", 80),
            )
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        return _to_result("read_ui_controls", outcome)


class TypeText(Tool):
    name = "type_text"
    description = (
        "Type text into whatever field currently has focus. Never use this for passwords: ask the user "
        "to type those themselves."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "maxLength": 10000, "description": "Text to type."},
            "secret": {"type": "boolean", "description": "Do not echo the text back in logs (still not a substitute for the user typing it)."},
        },
        "required": ["text"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_keyboard_mouse"
    timeout_s = 60.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        text = str(arguments.get("text", ""))
        if len(text) > 4000:
            raise ToolError("That text is too long to type in one go.", hint="Split it into smaller pieces.")
        try:
            outcome = controller.type_text(text, secret=bool(arguments.get("secret", False)))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("type_text", outcome, data_extra={"characters": len(text)})


class PressKeys(Tool):
    name = "press_keys"
    description = (
        "Send a key or shortcut to the focused window, e.g. 'enter', 'tab', 'ctrl+s', 'alt+tab', "
        "'ctrl+shift+t'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "keys": {"type": "string", "description": "Key or '+'-joined combination.", "maxLength": 60},
            "presses": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["keys"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_keyboard_mouse"
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.press_keys(str(arguments.get("keys", "")), presses=int_arg(arguments, "presses", 1))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("press_keys", outcome)


class ClickControl(Tool):
    name = "click_control"
    description = (
        "Click a named control (button, link, tab) inside a window. Prefer this over coordinate clicks: "
        "it can be verified afterwards."
    )
    parameters = {
        "type": "object",
        "properties": {
            "control": {"type": "string", "description": "The control's visible label."},
            "window": {"type": "string", "description": "Part of the window title. Defaults to the active window."},
            "exact": {"type": "boolean", "description": "Require an exact label match."},
        },
        "required": ["control"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_keyboard_mouse"
    permission_action = "click_control"
    timeout_s = 30.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.click_element(
                str(arguments.get("control", "")),
                window=str(arguments.get("window", "")),
                exact=bool(arguments.get("exact", False)),
            )
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("click_control", outcome)


class ClickCoordinates(Tool):
    name = "click_coordinates"
    description = (
        "Click at a specific screen position. This is a last resort: the click cannot be verified, so "
        "describe why the target has no accessible control, and only use it after a screenshot."
    )
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "minimum": 0, "maximum": 20000},
            "y": {"type": "integer", "minimum": 0, "maximum": 20000},
            "reason": {"type": "string", "description": "Why a coordinate click is necessary.", "maxLength": 300},
        },
        "required": ["x", "y", "reason"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_keyboard_mouse"
    timeout_s = 20.0

    def confirm_required(self, arguments: dict[str, Any], context: ToolContext) -> bool:
        return True

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        request = super().confirmation_details(arguments, context)
        request.title = "Click at a screen position?"
        request.details = f"Click at ({arguments.get('x')}, {arguments.get('y')})"
        request.preview = (
            f"Reason given: {arguments.get('reason')}\n\n"
            "A coordinate click cannot be verified - LUMI cannot tell what it actually hit."
        )
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.click_at(int_arg(arguments, "x", 0), int_arg(arguments, "y", 0))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("click_coordinates", outcome)


class ScrollScreen(Tool):
    name = "scroll"
    description = "Scroll the focused window up or down."
    parameters = {
        "type": "object",
        "properties": {
            "amount": {"type": "integer", "minimum": -50, "maximum": 50, "description": "Positive scrolls up, negative down (pyautogui units)."},
        },
        "required": ["amount"],
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_keyboard_mouse"
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.scroll(int_arg(arguments, "amount", -3))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        except AutomationStopped as exc:
            raise ToolError(exc.message)
        return _to_result("scroll", outcome)


class TakeScreenshot(Tool):
    name = "take_screenshot"
    description = (
        "Save a screenshot to the LUMI cache folder so you can look at the screen. Use it when the "
        "user asks what is on screen or when an element cannot be found by name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 80, "description": "Optional file name."},
        },
    }
    category = "computer"
    risk = RiskLevel.MODERATE
    requires_permission = "allow_screenshots"
    timeout_s = 30.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.screenshot(name=str(arguments.get("name", "")))
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        return _to_result("take_screenshot", outcome)


class ActiveWindow(Tool):
    name = "active_window"
    description = "Which window currently has focus (useful before typing or pressing keys)."
    parameters = {"type": "object", "properties": {}}
    category = "computer"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        controller = _controller(context)
        try:
            outcome = controller.active_window()
        except AutomationUnavailable as exc:
            raise ToolError(exc.message, hint=exc.hint)
        return _to_result("active_window", outcome)


def build_tools() -> Iterable[Tool]:
    return (
        OpenApplication(),
        OpenPath(),
        ListOpenWindows(),
        FocusWindow(),
        ReadWindowText(),
        ReadUiControls(),
        TypeText(),
        PressKeys(),
        ClickControl(),
        ClickCoordinates(),
        ScrollScreen(),
        TakeScreenshot(),
        ActiveWindow(),
    )
