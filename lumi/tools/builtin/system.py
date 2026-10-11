"""System and model-management tools.

These keep the local AI engine honest: check whether Ollama is reachable, look at
what is installed, explain what will fit on this machine, download only with
consent, and unload a model when idle.  Also exposes the local activity log so the
user can always see what LUMI did.
"""

from __future__ import annotations

from typing import Any, Iterable

from lumi.ai import models as model_catalogue
from lumi.ai.hardware import current_load, detect as detect_hardware
from lumi.ai.ollama import OllamaError, estimate_seconds_for_pull
from lumi.tools.base import ConfirmationRequest, RiskLevel, Tool, ToolContext, ToolError, ToolResult
from lumi.tools.builtin._common import SERVICE_ENGINE, int_arg, ok, service


class OllamaStatusTool(Tool):
    name = "ollama_status"
    description = (
        "Check whether the local AI engine (Ollama) is running, which model is selected, and what to "
        "tell the user if something is missing."
    )
    parameters = {"type": "object", "properties": {}}
    category = "system"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        engine = context.services.get(SERVICE_ENGINE)
        if engine is None:
            raise ToolError("The assistant engine is not available in this session.")
        status = engine.status()
        selected = context.settings.model.selected_model or "(none selected)"
        if status.ready:
            summary = f"Ollama {status.version or ''} is running at {status.base_url}. Selected model: {selected}."
        else:
            summary = (
                f"Ollama is not reachable ({status.message}). "
                "LUMI still handles tasks, notes, reminders and files without it."
            )
        return ok(summary, data={"status": status.to_dict(), "selected_model": selected}, verified=True)


class ListLocalModels(Tool):
    name = "list_local_models"
    description = "List the models installed in Ollama, with their size and whether they support tool calling."
    parameters = {"type": "object", "properties": {}}
    category = "system"
    risk = RiskLevel.SAFE
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        engine = context.services.get(SERVICE_ENGINE)
        if engine is None:
            raise ToolError("The assistant engine is not available in this session.")
        try:
            models = engine.list_models()
        except OllamaError as exc:
            raise ToolError(exc.friendly())
        if not models:
            return ok(
                "No models are installed in Ollama yet.",
                data={"models": []},
                verified=True,
            )
        lines = []
        for model in models:
            capability = ""
            if model.supports_tools:
                capability = " · tool calling"
            elif model.capabilities:
                capability = " · " + ", ".join(model.capabilities[:2])
            lines.append(f"- {model.name} ({model.size_human}{capability})")
        return ok(
            f"{len(models)} model(s) installed:\n" + "\n".join(lines),
            data={"models": [model.to_dict() for model in models]},
            verified=True,
        )


class RecommendModels(Tool):
    name = "recommend_models"
    description = (
        "Suggest models that suit this machine's RAM and GPU, with the honest trade-offs and download "
        "sizes. Use before suggesting a download."
    )
    parameters = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 8}},
    }
    category = "system"
    risk = RiskLevel.SAFE
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        engine = context.services.get(SERVICE_ENGINE)
        installed: list[str] = []
        if engine is not None:
            try:
                installed = [model.name for model in engine.list_models()]
            except OllamaError:
                installed = []
        hardware = detect_hardware()
        recommendations = model_catalogue.recommendations(
            limit=int_arg(arguments, "limit", 4), installed=installed, hardware=hardware
        )
        if not recommendations:
            return ok(
                "Every suitable model is already installed.",
                data={"recommendations": [], "hardware": hardware.to_dict()},
                verified=True,
            )
        lines = []
        for item in recommendations:
            lines.append(
                f"- {item['name']} ({item['size_human']}, {item['parameters']}): {item['purpose']}\n"
                f"    fit: {item['fit']['message']}"
            )
            if item["weaknesses"]:
                lines.append(f"    weaker at: {'; '.join(item['weaknesses'])}")
        return ok(
            f"Hardware: {hardware.summary_line()}\n\n" + "\n".join(lines),
            data={"recommendations": recommendations, "hardware": hardware.to_dict()},
            verified=True,
        )


class PullModel(Tool):
    name = "pull_model"
    description = (
        "Download a model into Ollama. This uses disk space and bandwidth, so it always asks first and "
        "the user can cancel. Never called without the user asking for a specific model."
    )
    parameters = {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Model tag, e.g. llama3.2:3b", "maxLength": 120},
        },
        "required": ["model"],
    }
    category = "system"
    risk = RiskLevel.CONSEQUENTIAL
    always_confirm = True
    confirmation_title = "Download this model?"
    permission_action = "install_software"
    timeout_s = None  # downloads are long; cancellation is cooperative
    examples = ("download llama3.2:3b",)

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        request = super().confirmation_details(arguments, context)
        model = str(arguments.get("model", ""))
        hardware = detect_hardware()
        size = model_catalogue.estimate_size_from_name(model)
        fit = hardware.fit_for_model(size) if size else None
        request.details = f"Download {model} ({model_catalogue.find_suggestion(model).size_human if model_catalogue.find_suggestion(model) else 'size unknown'})"
        lines = [f"Estimated download: {model_catalogue.find_suggestion(model).size_human if model_catalogue.find_suggestion(model) else 'unknown'}"]
        if size:
            lines.append(f"Estimated time on a 20 Mbit/s connection: {estimate_seconds_for_pull(size, 20)}")
        if fit:
            lines.append(fit["message"])
        lines.append(f"Free disk space: {hardware.disk_free_gb:.0f} GB")
        lines.append("You can cancel the download at any time with the Stop button.")
        request.preview = "\n".join(lines)
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        engine = context.services.get(SERVICE_ENGINE)
        if engine is None:
            raise ToolError("The assistant engine is not available in this session.")
        model = str(arguments.get("model", "")).strip()
        if not model:
            raise ToolError("Which model should I download?")
        hardware = detect_hardware()
        size = model_catalogue.estimate_size_from_name(model)
        if size and hardware.disk_free_gb and size / (1024 ** 3) > hardware.disk_free_gb - 2:
            raise ToolError(
                f"There is not enough free disk space for '{model}' "
                f"(needs about {size / 1024**3:.1f} GB, you have {hardware.disk_free_gb:.0f} GB free).",
                hint="Free some space or pick a smaller model such as llama3.2:3b.",
            )

        progress_notes: list[str] = []

        def on_progress(payload: dict[str, Any]) -> None:
            status = payload.get("status")
            if status and len(progress_notes) < 40:
                completed = payload.get("completed")
                total = payload.get("total")
                if completed and total:
                    progress_notes.append(f"{status} {int(completed / total * 100)}%")
                else:
                    progress_notes.append(str(status))
            context.check_cancelled("downloading a model")

        try:
            engine.client().pull_model(model, on_progress=on_progress, stop_token=context.stop_token)
        except OllamaError as exc:
            if exc.kind == "cancelled":
                return ToolResult.cancelled("Model download cancelled.")
            raise ToolError(exc.friendly())
        return ok(
            f"Model '{model}' is ready. Select it in the model list to start using it.",
            data={"model": model, "progress": progress_notes[-5:]},
            verified=True,
        )


class UnloadModel(Tool):
    name = "unload_model"
    description = (
        "Ask Ollama to unload a model from memory. Useful to free VRAM/RAM before a game or a heavy "
        "build, and to stop the fans spinning."
    )
    parameters = {
        "type": "object",
        "properties": {"model": {"type": "string", "maxLength": 120, "description": "Defaults to the selected model."}},
    }
    category = "system"
    risk = RiskLevel.MODERATE
    timeout_s = 30.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        engine = context.services.get(SERVICE_ENGINE)
        if engine is None:
            raise ToolError("The assistant engine is not available in this session.")
        model = str(arguments.get("model") or context.settings.model.selected_model or "").strip()
        if not model:
            raise ToolError("No model is selected, so there is nothing to unload.")
        loaded = engine.client().unload_model(model)
        if not loaded:
            return ok(f"I asked Ollama to unload {model} (it may already have been unloaded).", verified=None)
        return ok(f"Unloaded {model} from memory.", data={"model": model}, verified=True)


class SystemHealth(Tool):
    name = "system_health"
    description = "CPU, memory and disk usage right now, plus whether the local model engine is up."
    parameters = {"type": "object", "properties": {}}
    category = "system"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        hardware = detect_hardware()
        load = current_load()
        engine = context.services.get(SERVICE_ENGINE)
        ollama_line = "Ollama: not checked"
        if engine is not None:
            status = engine.status()
            ollama_line = f"Ollama: {'running ' + (status.version or '') if status.ready else 'not reachable'}"
        summary = (
            f"CPU {load['cpu_percent']:.0f}% · RAM {load['ram_percent']:.0f}% "
            f"({hardware.ram_available_gb:.1f} GB free) · disk {hardware.disk_free_gb:.0f} GB free. {ollama_line}."
        )
        return ok(summary, data={"load": load, "hardware": hardware.to_dict()}, verified=True)


class ReadActivity(Tool):
    name = "read_activity"
    description = (
        "Show the recent tool activity log: what LUMI did, when, and whether it succeeded. "
        "Use it when the user asks 'what have you done?'"
    )
    parameters = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
    }
    category = "system"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        audit = context.audit
        if audit is None:
            return ok("No activity log is available in this session.", verified=True)
        limit = int_arg(arguments, "limit", 25)
        entries = audit.recent_from_db(limit=limit)
        if not entries:
            return ok("Nothing has been recorded yet.", data={"entries": []}, verified=True)
        lines = [entry.human_line() for entry in reversed(entries)]
        return ok("\n".join(lines), data={"entries": [entry.to_dict() for entry in entries]}, verified=True)


class ReadLogs(Tool):
    name = "read_logs"
    description = (
        "Read the end of LUMI's own log file. Logs stay on this machine and credentials are redacted "
        "before they are written."
    )
    parameters = {
        "type": "object",
        "properties": {"lines": {"type": "integer", "minimum": 5, "maximum": 500}},
    }
    category = "system"
    risk = RiskLevel.SAFE
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        paths = context.paths
        log_file = getattr(paths, "log_file", None)
        if log_file is None:
            raise ToolError("I cannot find the log file location.")
        from pathlib import Path

        path = Path(log_file)
        if not path.exists():
            return ok("The log file does not exist yet.", verified=True)
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise ToolError(f"Could not read the log file: {exc}")
        tail = content[-int_arg(arguments, "lines", 60):]
        return ok(
            f"Last {len(tail)} lines of {path.name}:\n\n" + "\n".join(tail),
            data={"path": str(path), "lines": tail},
            verified=True,
        )


class ClearLogs(Tool):
    name = "clear_logs"
    description = "Delete LUMI's local log file. Asks first."
    parameters = {"type": "object", "properties": {}}
    category = "system"
    risk = RiskLevel.CONSEQUENTIAL
    always_confirm = True
    confirmation_title = "Clear LUMI's logs?"
    permission_action = "delete_data"
    timeout_s = 15.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from pathlib import Path

        log_file = getattr(context.paths, "log_file", None)
        if log_file is None:
            raise ToolError("I cannot find the log file location.")
        path = Path(log_file)
        try:
            if path.exists():
                path.write_text("", encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Could not clear the log file: {exc}")
        return ok("Log file cleared.", data={"path": str(path)}, verified=True)


def build_tools() -> Iterable[Tool]:
    return (
        OllamaStatusTool(),
        ListLocalModels(),
        RecommendModels(),
        PullModel(),
        UnloadModel(),
        SystemHealth(),
        ReadActivity(),
        ReadLogs(),
        ClearLogs(),
    )
