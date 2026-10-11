"""Small information tools that must never need a model.

Time, date, hardware and "what can you do?" are all answered with ordinary
Python.  This is how LUMI stays useful when Ollama is not running.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from lumi.ai import models as model_catalogue
from lumi.ai.hardware import detect as detect_hardware
from lumi.tools.base import RiskLevel, Tool, ToolContext, ToolResult
from lumi.tools.builtin._common import ok
from lumi.version import APP_NAME, __version__
from lumi.utils.timeutil import now as current_time, to_iso


class CurrentDateTime(Tool):
    name = "current_datetime"
    description = (
        "The exact local date and time on this computer, plus the weekday. Use it instead of "
        "guessing what 'today' or 'tomorrow' means."
    )
    parameters = {"type": "object", "properties": {}}
    category = "info"
    risk = RiskLevel.SAFE
    timeout_s = 5.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        moment = current_time()
        text = moment.strftime("%A, %d %B %Y at %H:%M")
        return ok(
            text,
            data={
                "datetime": to_iso(moment),
                "weekday": moment.strftime("%A"),
                "date": moment.strftime("%Y-%m-%d"),
                "time": moment.strftime("%H:%M"),
                "timezone": datetime.now().astimezone().tzname() or "local",
            },
            verified=True,
        )


class HardwareInfo(Tool):
    name = "hardware_info"
    description = (
        "Report this computer's CPU, RAM, GPU and free disk space, and what that means for running "
        "local models. Honest limits, no guesses."
    )
    parameters = {"type": "object", "properties": {}}
    category = "info"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        hardware = detect_hardware()
        limits = model_catalogue.describe_hardware_limits(hardware)
        gpu = hardware.primary_gpu
        lines = [
            f"CPU: {hardware.cpu} ({hardware.physical_cores or hardware.logical_cores} cores)",
            f"RAM: {hardware.ram_total_gb:.1f} GB total, {hardware.ram_available_gb:.1f} GB free",
            f"GPU: {gpu.name + f' ({gpu.vram_gb} GB VRAM)' if gpu else 'none detected'}",
            f"Disk free: {hardware.disk_free_gb:.0f} GB",
        ]
        return ok("\n".join(lines) + "\n\n" + "\n".join(f"- {line}" for line in limits), data=hardware.to_dict(), verified=True)


class Capabilities(Tool):
    name = "capabilities"
    description = (
        "Explain what LUMI can do right now and what is switched off, based on the user's own "
        "settings and the installed dependencies. Use it when the user asks what you can do, or "
        "why something is unavailable."
    )
    parameters = {
        "type": "object",
        "properties": {
            "include_tools": {"type": "boolean", "description": "List the individual tools (default false)."},
        },
    }
    category = "info"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        automation = context.services.get("automation")
        executor = context.services.get("executor")
        included = bool(arguments.get("include_tools", False))

        lines = [
            f"{APP_NAME} {__version__} - what works right now",
            "",
            "Always available (no AI, no internet):",
            "- tasks, deadlines, repeats and the daily briefing",
            "- reminders with Windows notifications, including after a restart",
            "- notes, search, pinning and export",
            "- local memory you can read, edit and delete",
            "- file search, reading and (when allowed) writing inside your folders",
        ]
        lines.append("")
        lines.append("Needs your permission (Settings → Permissions):")
        lines.append(f"- open applications: {'on' if settings.automation.allow_app_launch else 'off'}")
        lines.append(f"- keyboard and mouse control: {'on' if settings.automation.allow_keyboard_mouse else 'off'}")
        lines.append(f"- screenshots: {'on' if settings.automation.allow_screenshots else 'off'}")
        lines.append(f"- writing files: {'on' if settings.automation.allow_file_writes else 'off'}")
        lines.append(f"- browser automation (Gmail drafts): {'on' if settings.automation.allow_browser_automation else 'off'}")
        lines.append("")
        lines.append("Intelligence:")
        engine = context.services.get("engine")
        if engine is not None:
            status = engine.status()
            if status.ready:
                model = settings.model.selected_model or "(none selected)"
                lines.append(f"- Ollama {status.version or ''} is running; selected model: {model}")
            else:
                lines.append(f"- Ollama is not reachable: {status.message}")
        lines.append(
            "- A local model answers questions, explains your notes and writes text. It cannot browse "
            "the web by itself, and it is smaller than cloud models, so it can be wrong."
        )
        lines.append("")
        lines.append("Not enabled / not supported:")
        lines.append("- reading your passwords, cookies or browser sessions: never")
        lines.append("- running shell commands or model-generated code: never")
        lines.append("- cloud AI (Gemini/OpenAI): off unless you configure a key")
        lines.append("- calendar: only if you connect a local integration yourself")

        tools_payload: list[dict[str, Any]] = []
        if included and executor is not None:
            describe = executor.describe_available(context)
            tools_payload = list(describe["available"])
            lines.append("")
            lines.append("Tools available now:")
            for entry in describe["available"]:
                risk = entry["risk"]
                verdict = entry["decision"]["verdict"]
                lines.append(f"- {entry['name']} [{risk}, {verdict}] {entry['description'][:110]}")
            if describe["unavailable"]:
                lines.append("")
                lines.append("Tools unavailable:")
                for entry in describe["unavailable"]:
                    lines.append(f"- {entry['name']}: {entry['reason']}")

        automation_note = ""
        if automation is not None:
            status = automation.status()
            if not status["desktop_ready"]:
                automation_note = f"\n\nAutomation limitation: {status['reason']}"
        return ok(
            "\n".join(lines) + automation_note,
            data={
                "settings_status": automation.status() if automation else {},
                "tools": tools_payload,
                "available": bool(tools_payload),
            },
            verified=True,
        )


def build_tools() -> Iterable[Tool]:
    return (CurrentDateTime(), HardwareInfo(), Capabilities())
