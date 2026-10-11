"""Tool execution: validation, permission gating, confirmation, timeouts, audit.

These are the tests that matter most: they prove that the assistant cannot run
anything that has not been validated, permitted and (where required) confirmed.
"""

from __future__ import annotations

import json
import time

import jsonschema
import pytest

from lumi.security.audit import AuditLog
from lumi.security.policy import PermissionPolicy
from lumi.tools.base import (
    ConfirmationRequest,
    RiskLevel,
    StopToken,
    Tool,
    ToolContext,
    ToolResult,
)
from lumi.tools.executor import ToolCall, ToolExecutor, parse_tool_arguments
from lumi.tools.registry import ToolRegistry
from lumi.tools.validation import coerce_arguments, summarise_errors, validate_arguments


# ---------------------------------------------------------------------------
# small tools used to exercise the executor in isolation
# ---------------------------------------------------------------------------
class EchoTool(Tool):
    name = "echo"
    description = "Return the text you were given."
    category = "test"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }

    def run(self, arguments, context):
        return ToolResult.success("echoed", data={"text": arguments["text"]})


class SlowTool(Tool):
    name = "slow"
    description = "Sleep until the stop token is set."
    category = "test"
    parameters = {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": []}
    timeout_s = 1.0

    def run(self, arguments, context):
        deadline = time.monotonic() + float(arguments.get("seconds", 30))
        while time.monotonic() < deadline:
            if context.stop_token.cancelled:
                return ToolResult.cancelled("stopped by the user")
            time.sleep(0.02)
        return ToolResult.success("finished")


class BoomTool(Tool):
    name = "boom"
    description = "Always raises."
    category = "test"
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, arguments, context):
        raise RuntimeError("unexpected explosion")


class DangerTool(Tool):
    name = "danger"
    description = "A consequential action."
    category = "test"
    parameters = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}
    risk = RiskLevel.CONSEQUENTIAL
    permission_action = "allow_app_launch"

    def run(self, arguments, context):
        return ToolResult.success("danger ran", data={"target": arguments["target"]})


class GatedTool(Tool):
    name = "gated"
    description = "Needs the screenshot permission."
    category = "test"
    parameters = {"type": "object", "properties": {}, "required": []}
    risk = RiskLevel.MODERATE
    requires_permission = "allow_screenshots"

    def run(self, arguments, context):
        return ToolResult.success("gated ran")


class BrokenTool(Tool):
    name = "broken"
    description = "Returns a non-result."
    category = "test"
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, arguments, context):
        return "not a ToolResult"


@pytest.fixture()
def small_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all([EchoTool(), SlowTool(), BoomTool(), DangerTool(), GatedTool(), BrokenTool()])
    return registry


@pytest.fixture()
def special_executor(small_registry: ToolRegistry, policy: PermissionPolicy, audit: AuditLog) -> ToolExecutor:
    return ToolExecutor(small_registry, policy, audit)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_every_builtin_schema_is_valid() -> None:
    for tool in ToolRegistry.build_default().all():
        jsonschema.Draft7Validator.check_schema(tool.parameters)


def test_builtin_schemas_disallow_unknown_arguments() -> None:
    for tool in ToolRegistry.build_default().all():
        assert tool.parameters.get("additionalProperties") is False, tool.name


def test_missing_required_argument_is_reported() -> None:
    errors = validate_arguments(EchoTool.parameters, {})
    assert errors and "text" in summarise_errors(errors)


def test_unknown_argument_is_rejected() -> None:
    errors = validate_arguments(EchoTool.parameters, {"text": "hi", "extra": 1})
    assert errors


def test_wrong_type_is_reported_clearly() -> None:
    errors = validate_arguments(EchoTool.parameters, {"text": 5})
    assert errors and "text" in summarise_errors(errors)


def test_oversized_payload_is_rejected() -> None:
    schema = {"type": "object", "properties": {"blob": {"type": "string"}}, "required": ["blob"]}
    errors = validate_arguments(schema, {"blob": "x" * 40_000})
    assert errors


def test_coercion_only_does_safe_conversions() -> None:
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "flag": {"type": "boolean"}, "text": {"type": "string"}},
    }
    coerced = coerce_arguments({"n": "3", "flag": "true", "text": "42"}, schema)
    assert coerced["n"] == 3
    assert coerced["flag"] is True
    # A numeric *string* stays a string: guessing here would corrupt file names.
    assert coerced["text"] == "42"


def test_json_string_arguments_are_unwrapped() -> None:
    parsed = parse_tool_arguments('{"text": "wrapped"}')
    assert parsed == {"text": "wrapped"}
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments("not json at all") == {}


# ---------------------------------------------------------------------------
# happy path and failure handling
# ---------------------------------------------------------------------------
def test_successful_execution(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = special_executor.execute(ToolCall("echo", {"text": "hello"}), tool_context)
    assert outcome.ok, outcome.message
    assert outcome.result.data["text"] == "hello"
    assert outcome.duration_ms >= 0


def test_unknown_tool_is_refused(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = special_executor.execute(ToolCall("sudo_everything", {}), tool_context)
    assert not outcome.ok
    assert outcome.status == "invalid"
    assert "not a tool" in outcome.message
    assert "echo" in outcome.message, "the error should list what it *can* do"


def test_invalid_arguments_never_reach_the_tool(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = special_executor.execute(ToolCall("echo", {"nope": 1}), tool_context)
    assert outcome.status == "invalid"
    assert not outcome.ok


def test_tool_exception_becomes_a_friendly_error(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = special_executor.execute(ToolCall("boom", {}), tool_context)
    assert outcome.status == "error"
    assert "Traceback" not in outcome.result.error
    assert "explosion" in outcome.result.error


def test_tool_must_return_a_result(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = special_executor.execute(ToolCall("broken", {}), tool_context)
    assert not outcome.ok
    assert "unexpected value" in outcome.result.summary
    assert "str" in outcome.result.error


def test_timeout_stops_the_wait(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    started = time.monotonic()
    outcome = special_executor.execute(ToolCall("slow", {"seconds": 30}), tool_context)
    elapsed = time.monotonic() - started
    assert outcome.status in {"timeout", "cancelled"}
    assert elapsed < 8, "the watchdog must return rather than hang"


def test_pre_cancelled_context_skips_the_tool(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.stop_token = StopToken()
    tool_context.stop_token.cancel()
    outcome = special_executor.execute(ToolCall("echo", {"text": "hi"}), tool_context)
    assert outcome.status == "cancelled"
    assert outcome.result.extra.get("cancelled") is True


# ---------------------------------------------------------------------------
# permissions and confirmation
# ---------------------------------------------------------------------------
def test_permission_flag_blocks_the_tool(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_screenshots = False
    outcome = special_executor.execute(ToolCall("gated", {}), tool_context)
    assert outcome.status == "denied"
    assert "allow_screenshots" in outcome.message
    assert "Settings" in outcome.result.hint


def test_permission_flag_allows_the_tool(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_screenshots = True
    outcome = special_executor.execute(ToolCall("gated", {}), tool_context)
    assert outcome.ok


def test_automation_disabled_blocks_moderate_tools(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.enabled = False
    tool_context.settings.automation.allow_screenshots = True
    outcome = special_executor.execute(ToolCall("gated", {}), tool_context)
    assert outcome.status == "denied"


def test_consequential_tool_asks_before_running(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_app_launch = True
    prompts: list[ConfirmationRequest] = []

    def confirm(request: ConfirmationRequest) -> bool:
        prompts.append(request)
        return True

    outcome = special_executor.execute(ToolCall("danger", {"target": "notepad"}), tool_context, confirmation_provider=confirm)
    assert outcome.ok, outcome.message
    assert len(prompts) == 1
    assert prompts[0].tool == "danger"
    assert "notepad" in prompts[0].preview


def test_without_a_confirmation_channel_nothing_runs(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_app_launch = True
    tool_context.confirm = None
    outcome = special_executor.execute(ToolCall("danger", {"target": "notepad"}), tool_context)
    assert outcome.status == "declined"
    assert "did not do anything" in outcome.result.summary


def test_declining_stops_the_action(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_app_launch = True
    outcome = special_executor.execute(
        ToolCall("danger", {"target": "notepad"}), tool_context, confirmation_provider=lambda request: False
    )
    assert outcome.status == "declined"


def test_confirmation_dialog_failure_is_treated_as_refusal(
    special_executor: ToolExecutor, tool_context: ToolContext
) -> None:
    def exploding(request: ConfirmationRequest) -> bool:
        raise RuntimeError("dialog crashed")

    tool_context.settings.automation.allow_app_launch = True
    outcome = special_executor.execute(ToolCall("danger", {"target": "notepad"}), tool_context, confirmation_provider=exploding)
    assert outcome.status == "declined"


def test_session_grant_is_remembered(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_app_launch = True
    calls: list[int] = []

    def allow_session(request: ConfirmationRequest) -> str:
        calls.append(1)
        return "session"

    first = special_executor.execute(ToolCall("danger", {"target": "notepad"}), tool_context, confirmation_provider=allow_session)
    second = special_executor.execute(ToolCall("danger", {"target": "notepad"}), tool_context, confirmation_provider=allow_session)
    assert first.ok and second.ok
    assert len(calls) == 1, "a session grant must not ask twice"
    assert second.decision is not None and second.decision.used_session_grant


def test_confirmation_can_be_globally_disabled(special_executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_app_launch = True
    asked: list[int] = []
    outcome = special_executor.execute(
        ToolCall("danger", {"target": "notepad"}),
        tool_context,
        allow_confirmation=False,
        confirmation_provider=lambda request: asked.append(1) or True,
    )
    assert outcome.status == "declined"
    assert asked == []


def test_shipped_defaults_keep_automation_off() -> None:
    from lumi.config.settings import Settings

    settings = Settings()
    for field in (
        "allow_app_launch",
        "allow_keyboard_mouse",
        "allow_screenshots",
        "allow_browser_automation",
        "allow_file_writes",
    ):
        assert getattr(settings.automation, field) is False, f"{field} must be granted by the user, not assumed"

    # A tool that needs a switched-off capability is denied, not silently run.
    policy = PermissionPolicy(settings)
    class LaunchTool(Tool):
        name = "open_application"
        description = "Launch an app."
        parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        requires_permission = "allow_app_launch"
        risk = RiskLevel.MODERATE

        def run(self, arguments, context):  # pragma: no cover - never runs
            return ToolResult.success("launched")

    decision = policy.evaluate(LaunchTool())
    assert decision.allowed is False
    assert "allow_app_launch" in decision.reason


def test_policy_rejects_unknown_permission_fields(settings) -> None:
    class WeirdTool(Tool):
        name = "weird"
        description = "unknown permission field"
        parameters = {"type": "object", "properties": {}}
        requires_permission = "allow_time_travel"

        def run(self, arguments, context):  # pragma: no cover - never runs
            return ToolResult.success("nope")

    decision = PermissionPolicy(settings).evaluate(WeirdTool())
    assert not decision.allowed, "an unknown permission field must fail closed"


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------
def test_audit_records_the_phases_of_a_call(special_executor: ToolExecutor, audit: AuditLog, tool_context: ToolContext) -> None:
    special_executor.execute(ToolCall("echo", {"text": "audited"}), tool_context)
    entries = [entry for entry in audit.recent(limit=20) if entry.tool == "echo"]
    statuses = [entry.status for entry in entries]
    assert "running" in statuses
    assert "ok" in statuses
    assert any(entry.permission == "allowed" for entry in entries)


def test_audit_records_denials(special_executor: ToolExecutor, audit: AuditLog, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_screenshots = False
    special_executor.execute(ToolCall("gated", {}), tool_context)
    entries = [entry for entry in audit.recent(limit=20) if entry.tool == "gated"]
    assert any(entry.status == "denied" for entry in entries)
    assert any("allow_screenshots" in entry.message for entry in entries)


def test_audit_redacts_secret_arguments(special_executor: ToolExecutor, audit: AuditLog, tool_context: ToolContext) -> None:
    special_executor.execute(ToolCall("echo", {"text": "api_key = sk-abcdefghijklmnopqrstuvwx"}), tool_context)
    rows = [entry for entry in audit.recent(limit=20) if entry.tool == "echo"]
    dumped = json.dumps([entry.args for entry in rows])
    assert "sk-abcdefghijklmnopqrstuvwx" not in dumped
    assert "redacted" in dumped


def test_audit_survives_a_reload(audit: AuditLog, db) -> None:
    audit.record("echo", status="ok", args={"text": "persisted"}, message="done")
    fresh = AuditLog(db, session_id="another-session")
    rows = fresh.recent_from_db(limit=5)
    assert rows and rows[0].tool == "echo"
    assert "persisted" in json.dumps(rows[0].args)
    back = AuditLog(db, session_id="third")
    summary = back.summary()
    assert summary.get("ok", 0) >= 1
    assert "echo" in AuditLog(db, session_id="fourth").format_text()


def test_audit_listeners_receive_entries(audit: AuditLog) -> None:
    seen: list[str] = []
    audit.add_listener(lambda entry: seen.append(entry.tool))
    audit.record("echo", status="running")
    assert seen == ["echo"]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_registry_lookup_and_listing(registry: ToolRegistry) -> None:
    assert registry.get("add_task") is not None
    assert registry.get("never_heard_of_it") is None
    names = registry.names()
    assert len(set(names)) == len(names)
    assert names  # deterministic insertion order is fine, duplicates are not
    assert registry.by_category()["tasks"]
    assert registry.stats()["total"] == len(registry)


def test_registry_schemas_are_model_ready(registry: ToolRegistry, tool_context: ToolContext) -> None:
    schemas = registry.schemas_for_model(tool_context)
    assert schemas
    first = schemas[0]
    assert first["type"] == "function"
    assert set(first["function"]) == {"name", "description", "parameters"}
    assert first["function"]["name"]
    assert first["function"]["description"]


def test_registry_docs_are_compact(registry: ToolRegistry, tool_context: ToolContext) -> None:
    docs = registry.docs_for_prompt(tool_context)
    assert docs
    assert all(isinstance(line, str) and line for line in docs)
    assert any(line.startswith("add_task(") for line in docs)


def test_registry_validate_call(registry: ToolRegistry) -> None:
    assert registry.validate_call("add_task", {"title": "x"}) == []
    problems = registry.validate_call("add_task", {})
    assert problems and "title" in problems[0]
    assert registry.validate_call("nope", {}) != []


def test_duplicate_registration_is_rejected(small_registry: ToolRegistry) -> None:
    with pytest.raises(ValueError):
        small_registry.register(EchoTool())


def test_registry_unregister(small_registry: ToolRegistry) -> None:
    assert small_registry.unregister("echo") is True
    assert small_registry.get("echo") is None
    assert small_registry.unregister("echo") is False


def test_registry_instances_are_shared(registry: ToolRegistry) -> None:
    # Lazy loading must not create two copies of the same tool.
    assert registry.get("add_task") is registry.get("add_task")


def test_planner_tools_are_compact(registry: ToolRegistry, tool_context: ToolContext) -> None:
    planner = registry.planner_tools(tool_context)
    assert planner
    assert set(planner[0]) == {"name", "description", "parameters", "risk"}
    assert len(json.dumps(planner)) <= len(json.dumps(registry.schemas_for_model(tool_context)))


def test_registry_availability_reports_reason(registry: ToolRegistry, tool_context: ToolContext) -> None:
    available = registry.available(tool_context)
    assert available
    for tool, reason in registry.unavailable(tool_context):
        assert reason, f"{tool.name} must explain why it is unavailable"
