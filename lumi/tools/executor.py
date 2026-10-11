"""Tool execution pipeline.

    user request
      -> model proposal (validated JSON)
      -> schema validation + argument coercion
      -> permission decision (lumi.security.policy)
      -> confirmation when required
      -> execution with timeout + cancellation
      -> audit record
      -> result handed back to the model

Nothing bypasses this class.  The executor never raises to the caller: every
outcome, including "the model asked for a tool that does not exist", comes back
as an :class:`ExecutionOutcome` the UI can render and the model can read.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from lumi.security.policy import PermissionDecision, PermissionPolicy
from lumi.tools.base import (
    ConfirmationRequest,
    RiskLevel,
    StopToken,
    Tool,
    ToolCancelled,
    ToolContext,
    ToolError,
    ToolResult,
)
from lumi.tools.registry import ToolRegistry
from lumi.tools.validation import coerce_arguments, summarise_errors, validate_arguments

log = logging.getLogger("lumi.tools.executor")


@dataclass
class ToolCall:
    """A validated request to run one tool."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    source: str = "model"  # model | user | system | plan
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments, "source": self.source}


@dataclass
class ExecutionOutcome:
    """Everything that happened for one tool call."""

    tool: str
    status: str  # ok | error | invalid | denied | declined | cancelled | timeout | unavailable
    result: ToolResult
    decision: Optional[PermissionDecision] = None
    duration_ms: int = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.result.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "result": self.result.to_dict(),
            "permission": self.decision.to_dict() if self.decision else None,
        }

    def model_payload(self) -> str:
        return self.result.to_payload()


_CONFIRM_YES = {"yes", "y", "true", "allow", "approve", "ok", "confirm", "always", "session", "allow_session"}
_CONFIRM_NO = {"no", "n", "false", "deny", "denied", "cancel", "stop", "decline"}


def _interpret_confirmation(value: Any) -> tuple[bool, bool]:
    """Return ``(approved, session_grant)``."""
    if isinstance(value, bool):
        return value, False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"session", "always", "allow_session", "dont_ask_again"}:
            return True, True
        if lowered in _CONFIRM_YES:
            return True, False
        if lowered in _CONFIRM_NO:
            return False, False
        return False, False
    return bool(value), False


class ToolExecutor:
    """Runs tool calls with validation, permissions, timeouts and auditing."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        audit: Any = None,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.audit = audit
        self._on_event = on_event

    # --------------------------------------------------------------- events
    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:  # pragma: no cover - a UI listener must not break execution
            log.debug("tool event listener failed", exc_info=True)

    # -------------------------------------------------------------- propose
    def prepare(self, call: ToolCall, context: ToolContext) -> tuple[Optional[Tool], dict[str, Any], list[str], Optional[PermissionDecision]]:
        """Validate a call.  Returns tool, cleaned arguments, errors, decision."""
        tool = self.registry.get(call.name)
        if tool is None:
            known = ", ".join(sorted(self.registry.names())[:15])
            return None, {}, [f"'{call.name}' is not a tool LUMI knows. Available tools: {known}"], None

        available, reason = self.registry.availability(tool, context)
        if not available:
            return tool, {}, [reason or f"{tool.name} is unavailable right now."], None

        arguments = coerce_arguments(call.arguments or {}, tool.parameters)
        errors = validate_arguments(tool.parameters, arguments)
        if errors:
            return tool, arguments, errors, None

        decision = self.policy.evaluate(tool, arguments)
        return tool, arguments, [], decision

    # -------------------------------------------------------------- execute
    def execute(
        self,
        call: ToolCall,
        context: ToolContext,
        *,
        allow_confirmation: bool = True,
        confirmation_provider: Callable[[ConfirmationRequest], Any] | None = None,
    ) -> ExecutionOutcome:
        started = time.perf_counter()
        tool, arguments, errors, decision = self.prepare(call, context)

        if tool is None:
            message = summarise_errors(errors) or "unknown tool"
            self._audit(call.name, "denied", arguments, "invalid", message)
            self._emit("tool_invalid", {"tool": call.name, "errors": errors})
            return ExecutionOutcome(
                tool=call.name,
                status="invalid",
                result=ToolResult.failure(
                    f"I cannot run '{call.name}' because it is not a registered tool.",
                    hint="I can only use the tools listed in my instructions.",
                ),
                duration_ms=_elapsed_ms(started),
                message=message,
            )

        if errors:
            message = summarise_errors(errors)
            self._audit(tool.name, "denied", arguments, "invalid-arguments", message)
            self._emit("tool_invalid", {"tool": tool.name, "errors": errors, "arguments": arguments})
            return ExecutionOutcome(
                tool=tool.name,
                status="invalid",
                result=ToolResult.failure(
                    f"The arguments for {tool.name} were not valid.",
                    error=message,
                    hint="The request must match the tool's schema exactly.",
                ),
                duration_ms=_elapsed_ms(started),
                message=message,
            )

        assert decision is not None
        # A tool can know better than a blanket rule: overwriting an existing
        # file needs a question even though "write a file" is not consequential.
        needs_confirmation = decision.needs_confirmation
        if decision.allowed and not needs_confirmation and not decision.used_session_grant:
            try:
                needs_confirmation = bool(tool.confirm_required(arguments, context))
            except Exception:  # pragma: no cover - a broken hook must fail closed
                log.warning("confirm_required() failed for %s", tool.name, exc_info=True)
                needs_confirmation = True

        if not decision.allowed:
            self._audit(tool.name, "denied", arguments, decision.reason, decision.reason)
            self._emit("tool_denied", {"tool": tool.name, "reason": decision.reason})
            return ExecutionOutcome(
                tool=tool.name,
                status="denied",
                result=ToolResult.failure(
                    f"I am not allowed to run {tool.name} right now.",
                    error=decision.reason,
                    hint="You can change this in Settings → Permissions.",
                ),
                decision=decision,
                duration_ms=_elapsed_ms(started),
                message=decision.reason,
            )

        if needs_confirmation:
            approved, session_grant = self._ask(context, tool, arguments, confirmation_provider, allow_confirmation)
            if session_grant and approved:
                self.policy.grant_for_session(tool.name)
            if not approved:
                self._audit(tool.name, "denied", arguments, "user-declined", "You declined this action.")
                self._emit("tool_declined", {"tool": tool.name, "arguments": arguments})
                return ExecutionOutcome(
                    tool=tool.name,
                    status="declined",
                    result=ToolResult.failure(
                        f"You declined {tool.name}, so I did not do anything.",
                        error="declined by user",
                        hint="Tell me how you would like to proceed instead.",
                    ),
                    decision=decision,
                    duration_ms=_elapsed_ms(started),
                    message="declined by user",
                )

        self._audit(tool.name, "running", arguments, decision.verdict, "")
        self._emit("tool_started", {"tool": tool.name, "arguments": arguments, "risk": tool.risk.value})
        # A tool may need to ask its own follow-up question ("did you mean 18:00?").
        # It must use the *same* channel the executor used, so the user sees one
        # consistent dialog instead of a silent refusal.
        result = self._run_tool(tool, arguments, context, provider=confirmation_provider)
        result.tool = tool.name
        result.duration_ms = _elapsed_ms(started)

        status = "ok" if result.ok else "error"
        if result.extra.get("cancelled"):
            status = "cancelled"
        elif result.extra.get("timeout"):
            status = "timeout"
        self._audit(tool.name, status, arguments, decision.verdict, result.summary)
        self._emit(
            "tool_finished",
            {"tool": tool.name, "status": status, "summary": result.summary, "duration_ms": result.duration_ms},
        )
        return ExecutionOutcome(
            tool=tool.name,
            status=status,
            result=result,
            decision=decision,
            duration_ms=result.duration_ms,
            message=result.summary,
        )

    # ------------------------------------------------------------- helpers
    def _ask(
        self,
        context: ToolContext,
        tool: Tool,
        arguments: dict[str, Any],
        provider: Callable[[ConfirmationRequest], Any] | None,
        allow_confirmation: bool,
    ) -> tuple[bool, bool]:
        if not allow_confirmation or (provider is None and context.confirm is None):
            return False, False
        request = tool.confirmation_details(arguments, context)
        self._emit("tool_confirmation", {"request": request})
        handler = provider or context.confirm
        assert handler is not None
        try:
            outcome = handler(request)
        except Exception as exc:  # pragma: no cover - a failing dialog must not approve
            log.warning("confirmation dialog failed: %s", exc)
            return False, False
        return _interpret_confirmation(outcome)

    def _run_tool(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        provider: Callable[[ConfirmationRequest], Any] | None = None,
    ) -> ToolResult:
        """Run the tool with a timeout watchdog and a cooperative stop token.

        A tool may ask its own follow-up question ("did you mean 18:00?"), so the
        confirmation channel the executor was given is exposed on the context for
        the duration of the call: one dialog, one answer, no silent refusals.
        """
        if context.stop_token.cancelled:
            return ToolResult.cancelled()

        previous_confirm = context.confirm
        if provider is not None and context.confirm is None:
            context.confirm = provider

        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                box["result"] = tool.run(arguments, context)
            except ToolCancelled as exc:
                box["result"] = ToolResult.cancelled(str(exc))
            except ToolError as exc:
                box["result"] = ToolResult.failure(str(exc), error=exc.message, hint=exc.hint)
            except Exception as exc:  # noqa: BLE001 - a tool must never crash LUMI
                log.exception("tool %s raised", tool.name)
                box["result"] = ToolResult.failure(
                    f"{tool.name} hit an unexpected error and stopped.",
                    error=f"{type(exc).__name__}: {exc}",
                    hint="Nothing after this step was performed. Check the log for details.",
                )

        thread = threading.Thread(target=worker, name=f"tool-{tool.name}", daemon=True)
        try:
            thread.start()
            timeout = tool.timeout_s
            if timeout is None:
                thread.join()
            else:
                thread.join(timeout)
                if thread.is_alive():
                    # Ask cooperatively, then give up waiting.
                    context.stop_token.cancel()
                    thread.join(2.0)
                    log.warning("tool %s exceeded its %.1fs budget", tool.name, timeout)
                    result = box.get("result") or ToolResult.failure(
                        f"{tool.name} took longer than {timeout:.0f} seconds, so I stopped waiting.",
                        error="timeout",
                        hint="The action may or may not have completed - check the app you were working in.",
                    )
                    result.extra.setdefault("timeout", True)
                    return result
        finally:
            context.confirm = previous_confirm

        result = box.get("result")
        if result is None:  # pragma: no cover - defensive
            return ToolResult.failure(f"{tool.name} finished without reporting a result.", error="no result")
        if not isinstance(result, ToolResult):  # pragma: no cover - defensive
            return ToolResult.failure(
                f"{tool.name} returned an unexpected value.", error=f"bad result type: {type(result).__name__}"
            )
        return result

    def _audit(self, tool: str, status: str, arguments: dict[str, Any], permission: str, message: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(tool, status=status, args=arguments, permission=permission, message=message)
        except Exception:  # pragma: no cover - auditing is best effort
            log.debug("audit write failed", exc_info=True)

    # ------------------------------------------------------------ utilities
    def describe_available(self, context: ToolContext) -> dict[str, Any]:
        available = self.registry.available(context)
        unavailable = self.registry.unavailable(context)
        return {
            "available": [
                {
                    "name": tool.name,
                    "category": tool.category,
                    "risk": tool.risk.value,
                    "description": " ".join(tool.description.split()),
                    "decision": self.policy.evaluate(tool, {}).to_dict(),
                }
                for tool in available
            ],
            "unavailable": [{"name": tool.name, "reason": reason} for tool, reason in unavailable],
        }


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Accept dict, JSON string or empty payload for tool arguments."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


__all__ = ["ToolCall", "ToolExecutor", "ExecutionOutcome", "parse_tool_arguments", "StopToken", "ToolResult", "RiskLevel"]
