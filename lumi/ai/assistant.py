"""The assistant loop.

One user message in, one reply out, with the model and the tools working
together - and with a deterministic shortcut for the things that must never
depend on a model being available.

The order of work for each message:

1. **A pending question.**  If the previous turn asked "did you mean 18:00?",
   the answer is read here.
2. **Deterministic intents.**  "remind me to call mom at 6" is parsed locally and
   executed through the tool layer.  No model, no hallucination, works offline.
3. **The model.**  Local (or the user's own cloud provider), streaming, with
   native tool definitions when the model supports them and a strict JSON
   convention when it does not.
4. **Tool use.**  Every proposed call goes through the executor: validation,
   permissions, confirmation, audit.  The model never touches the system direct.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Protocol, Sequence

from lumi.ai import toolcalls
from lumi.ai.ollama import ChatChunk, OllamaClient, OllamaError, OllamaStatus
from lumi.ai.prompts import build_system_prompt, build_tool_result_message
from lumi.memory.service import ConversationStore, MemoryService, format_memory_lines
from lumi.tasks import nlp
from lumi.tools.base import StopToken, ToolContext
from lumi.tools.executor import ExecutionOutcome, ToolCall, ToolExecutor
from lumi.tools.registry import ToolRegistry
from lumi.utils.timeutil import to_iso

log = logging.getLogger("lumi.ai.assistant")

#: Default cap on model <-> tool round trips for one user message.
MAX_TOOL_ROUNDS = 3
#: How much conversation history is handed to the model.
HISTORY_MESSAGES = 14
HISTORY_CHARS = 6000
#: How many memories go into the prompt.
MEMORY_LIMIT = 8

_AFFIRM = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|correct|right|exactly|do it|please do|go ahead|confirm(ed)?|save it|add it)\b",
    re.I,
)
_NEGATIVE = re.compile(r"^\s*(no|nope|nah|don'?t|do not|stop|cancel|never ?mind|forget it|leave it)\b", re.I)
_REMEMBER_HINT = re.compile(r"\b(remember|note down|keep in mind|don'?t forget)\b.{3,}", re.I)
_CORRECTION = re.compile(r"\b(actually|instead|rather|no,? (?:make|set|use))\b", re.I)


@dataclass
class ChatCallbacks:
    """Everything the UI may want to show while a reply is produced."""

    on_status: Optional[Callable[[str], None]] = None
    on_token: Optional[Callable[[str], None]] = None
    on_reasoning: Optional[Callable[[str], None]] = None
    on_message_start: Optional[Callable[[str], None]] = None
    on_message_end: Optional[Callable[[str], None]] = None
    on_tool_proposed: Optional[Callable[[ToolCall], None]] = None
    on_tool_started: Optional[Callable[[str, dict[str, Any]], None]] = None
    on_tool_finished: Optional[Callable[[ExecutionOutcome], None]] = None
    on_memory_suggestion: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str, str], None]] = None

    def status(self, text: str) -> None:
        if self.on_status:
            _safe(self.on_status, text)

    def token(self, text: str) -> None:
        if self.on_token and text:
            _safe(self.on_token, text)

    def reasoning(self, text: str) -> None:
        if self.on_reasoning and text:
            _safe(self.on_reasoning, text)

    def message_start(self, model: str) -> None:
        if self.on_message_start:
            _safe(self.on_message_start, model)

    def message_end(self, text: str) -> None:
        if self.on_message_end:
            _safe(self.on_message_end, text)

    def tool_proposed(self, call: ToolCall) -> None:
        if self.on_tool_proposed:
            _safe(self.on_tool_proposed, call)

    def tool_started(self, name: str, arguments: dict[str, Any]) -> None:
        if self.on_tool_started:
            _safe(self.on_tool_started, name, arguments)

    def tool_finished(self, outcome: ExecutionOutcome) -> None:
        if self.on_tool_finished:
            _safe(self.on_tool_finished, outcome)

    def memory_suggestion(self, text: str) -> None:
        if self.on_memory_suggestion:
            _safe(self.on_memory_suggestion, text)

    def error(self, message: str, hint: str = "") -> None:
        if self.on_error:
            _safe(self.on_error, message, hint)


@dataclass
class PendingAction:
    """A question LUMI asked and is waiting on."""

    kind: str                      # "task" | "reminder"
    question: str
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


@dataclass
class AssistantReply:
    """The result of one user message."""

    text: str = ""
    model: str = ""
    provider: str = "local"
    tool_runs: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    hint: str = ""
    stopped: bool = False
    deterministic: bool = False
    pending_question: str = ""
    tokens_per_second: float = 0.0
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "tool_runs": self.tool_runs,
            "error": self.error,
            "hint": self.hint,
            "stopped": self.stopped,
            "deterministic": self.deterministic,
            "tokens_per_second": self.tokens_per_second,
            "duration_ms": self.duration_ms,
        }


@dataclass
class _ModelAnswer:
    text: str = ""
    error: str = ""
    hint: str = ""
    stopped: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens_per_second(self) -> float:
        try:
            return float(self.metrics.get("tokens_per_second") or 0.0)
        except (TypeError, ValueError):
            return 0.0


def _safe(callback: Callable[..., Any], *args: Any) -> None:
    try:
        callback(*args)
    except Exception:  # pragma: no cover - a broken UI callback must not stop a reply
        log.exception("chat callback failed")


class AssistantEngine:
    """Owns the conversation flow: fast path, model call, tool loop, memory."""

    def __init__(
        self,
        *,
        settings_store: Any,
        paths: Any,
        db: Any,
        registry: ToolRegistry,
        executor: ToolExecutor,
        memory_service: MemoryService,
        conversation_store: ConversationStore,
        audit: Any = None,
        client_factory: Callable[[Any], OllamaClient] | None = None,
        providers: Optional[dict[str, Any]] = None,
    ) -> None:
        self.settings_store = settings_store
        self.paths = paths
        self.db = db
        self.registry = registry
        self.executor = executor
        self.memory = memory_service
        self.conversations = conversation_store
        self.audit = audit
        self._client_factory = client_factory or self._default_client
        self._client: Optional[OllamaClient] = None
        self._client_key = ""
        self._providers = providers or {}
        self._pending: dict[int, PendingAction] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- plumbing
    @property
    def settings(self) -> Any:
        return self.settings_store.settings

    def _default_client(self, settings: Any) -> OllamaClient:
        return OllamaClient(
            settings.ollama.base_url,
            connect_timeout_s=settings.ollama.connect_timeout_s,
            read_timeout_s=settings.ollama.request_timeout_s,
            allow_non_loopback=settings.ollama.allow_non_loopback_host,
        )

    def client(self) -> OllamaClient:
        """A client for the *current* settings (rebuilt only when they change)."""
        settings = self.settings
        key = "|".join(
            str(value)
            for value in (
                settings.ollama.base_url,
                settings.ollama.connect_timeout_s,
                settings.ollama.request_timeout_s,
                settings.ollama.allow_non_loopback_host,
            )
        )
        with self._lock:
            if self._client is None or key != self._client_key:
                self._client = self._client_factory(settings)
                self._client_key = key
            return self._client

    def invalidate_model_cache(self) -> None:
        with self._lock:
            self._client = None
            self._client_key = ""

    def status(self) -> OllamaStatus:
        try:
            return self.client().status()
        except OllamaError as exc:
            return OllamaStatus(running=False, installed=True, message=exc.message, error_kind=exc.kind)
        except Exception as exc:  # pragma: no cover - defensive
            return OllamaStatus(running=False, installed=True, message=str(exc), error_kind="malformed")

    def list_models(self) -> list[Any]:
        return self.client().list_models()

    def model_names(self) -> list[str]:
        try:
            return self.client().model_names()
        except OllamaError:
            return []

    def selected_model(self) -> str:
        return (self.settings.model.selected_model or "").strip()

    def supports_tools(self, model: str) -> bool:
        if not model:
            return False
        try:
            return self.client().supports_tools(model)
        except OllamaError:
            return False

    def tools_enabled(self) -> bool:
        return bool(getattr(self.settings.model, "enable_tools", True))

    # --------------------------------------------------------------- public
    def send(
        self,
        user_text: str,
        *,
        conversation_id: Optional[int] = None,
        callbacks: Optional[ChatCallbacks] = None,
        stop_token: Optional[StopToken] = None,
        tool_context: Optional[ToolContext] = None,
        allow_tools: bool = True,
        save_history: bool = True,
    ) -> AssistantReply:
        """Handle one user message.  Never raises: failures become a reply."""
        started = time.perf_counter()
        callbacks = callbacks or ChatCallbacks()
        stop_token = stop_token or StopToken()
        message = (user_text or "").strip()
        if not message:
            return AssistantReply(error="I did not receive a message.", hint="Type something and press Enter.")
        if tool_context is None:
            return AssistantReply(
                error="LUMI is not fully wired up yet.",
                hint="Restart the application; if it keeps happening, check the log in the LUMI folder.",
            )

        allow_tools = allow_tools and self.tools_enabled()
        key = int(conversation_id or 0)

        # "Save conversations" is a real switch: with it off, neither side of the
        # exchange is written.  The assistant's reply already honoured it, so the
        # user's half used to be stored on its own.
        if save_history and conversation_id and self.settings.privacy.save_conversations:
            try:
                self.conversations.add_message(conversation_id, "user", message)
            except Exception:  # pragma: no cover - history is best effort
                log.debug("could not store the user message", exc_info=True)

        if self.settings.privacy.remember_from_chat:
            suggestion = _remember_suggestion(message)
            if suggestion:
                callbacks.memory_suggestion(suggestion)

        reply = AssistantReply()
        provider = self._choose_provider()
        reply.provider = provider

        # 1. answer a pending question from the previous turn
        pending = self._pending.pop(key, None)
        if pending is not None:
            if self._handle_pending_answer(pending, message, tool_context, callbacks, reply):
                return self._finish(reply, conversation_id, save_history, callbacks, started)
            if not _CORRECTION.search(message):
                callbacks.status("Starting a new request instead.")

        # 2. deterministic fast path (works with no model at all)
        if allow_tools and self.settings.productivity.deterministic_commands:
            if self._try_deterministic(message, tool_context, callbacks, reply, conversation_id, key):
                return self._finish(reply, conversation_id, save_history, callbacks, started)

        # 3. the model
        if stop_token.cancelled:
            reply.stopped = True
            reply.text = "Stopped before I started."
            return self._finish(reply, conversation_id, save_history, callbacks, started)

        answer = self._ask_model(message, conversation_id, callbacks, stop_token, tool_context, reply, allow_tools)
        reply.text = answer.text.strip()
        reply.stopped = answer.stopped
        reply.tokens_per_second = answer.tokens_per_second
        if answer.error:
            reply.error = answer.error
            reply.hint = answer.hint
            callbacks.error(reply.error, reply.hint)
        elif not reply.text and not reply.tool_runs and not reply.stopped:
            reply.text = (
                "The model returned an empty answer. That usually means the request was too long for its "
                "context window - try asking for a smaller piece."
            )
        return self._finish(reply, conversation_id, save_history, callbacks, started)

    def _finish(
        self,
        reply: AssistantReply,
        conversation_id: Optional[int],
        save_history: bool,
        callbacks: ChatCallbacks,
        started: float,
    ) -> AssistantReply:
        reply.duration_ms = int((time.perf_counter() - started) * 1000)
        self._store_reply(conversation_id, reply, save_history)
        callbacks.message_end(reply.text)
        return reply

    # ------------------------------------------------------ deterministic path
    def _try_deterministic(
        self,
        message: str,
        context: ToolContext,
        callbacks: ChatCallbacks,
        reply: AssistantReply,
        conversation_id: Optional[int],
        pending_key: int,
    ) -> bool:
        if context.stop_token.cancelled:
            return False
        parsed = nlp.parse_task_text(message)
        if parsed.intent == "unknown":
            return False

        is_reminder = parsed.remind_at is not None

        if parsed.intent == "add":
            if parsed.missing:
                question = self._question_for_missing(parsed)
                return self._ask(
                    reply, conversation_id, pending_key, "reminder" if is_reminder else "task", question, message, parsed
                )
            if parsed.needs_confirmation:
                question = nlp.describe_ambiguity(parsed)
                return self._ask(
                    reply, conversation_id, pending_key, "reminder" if is_reminder else "task", question, message, parsed
                )
            return self._run_parsed_add(parsed, is_reminder, context, callbacks, reply)

        if parsed.intent == "list":
            tool = "list_reminders" if _mentions_reminder(message) else "list_tasks"
            arguments = {"scope": "today"} if tool == "list_tasks" else {}
            return self._run_tool_text(tool, arguments, context, callbacks, reply)

        if parsed.intent in {"complete", "delete", "postpone"} and parsed.target_id is not None:
            arguments: dict[str, Any] = {"task_id": parsed.target_id}
            if parsed.intent == "postpone" and parsed.due_at is not None:
                arguments["when"] = to_iso(parsed.due_at)
            tool = {"complete": "complete_task", "delete": "delete_task", "postpone": "postpone_task"}[parsed.intent]
            return self._run_tool_text(tool, arguments, context, callbacks, reply)

        if parsed.intent in {"complete", "delete", "postpone"} and parsed.title:
            arguments = {"task": parsed.title}
            if parsed.intent == "postpone" and parsed.due_at is not None:
                arguments["when"] = to_iso(parsed.due_at)
            tool = {"complete": "complete_task", "delete": "delete_task", "postpone": "postpone_task"}[parsed.intent]
            return self._run_tool_text(tool, arguments, context, callbacks, reply)
        return False

    def _ask(
        self,
        reply: AssistantReply,
        conversation_id: Optional[int],
        pending_key: int,
        kind: str,
        question: str,
        original: str,
        parsed: nlp.ParseResult,
    ) -> bool:
        reply.text = question
        reply.deterministic = True
        reply.pending_question = question
        self._pending[pending_key] = PendingAction(
            kind=kind,
            question=question,
            text=parsed.text or original,
            payload=parsed.to_dict(),
            created_at=time.monotonic(),
        )
        if conversation_id:
            log.debug("waiting for confirmation on conversation %s", conversation_id)
        return True

    def _question_for_missing(self, parsed: nlp.ParseResult) -> str:
        missing = ", ".join(parsed.missing) or "a few details"
        verb = "remind you about" if parsed.remind_at else "add to your list"
        return f"Tell me what to {verb} and I will set it up (I am missing {missing})."

    def _run_parsed_add(
        self,
        parsed: nlp.ParseResult,
        is_reminder: bool,
        context: ToolContext,
        callbacks: ChatCallbacks,
        reply: AssistantReply,
    ) -> bool:
        if is_reminder:
            when = parsed.remind_at or parsed.due_at
            # Reminders carry no category - only the title, time and repeat rule.
            arguments: dict[str, Any] = {"title": parsed.title, "when": to_iso(when) if when else ""}
            if parsed.recurrence and parsed.recurrence != "none":
                arguments["recurrence"] = parsed.recurrence
            tool = "set_reminder"
        else:
            tool = "add_task_from_text"
            arguments = {"text": parsed.text}
        return self._run_tool_text(tool, arguments, context, callbacks, reply)

    def _run_tool_text(
        self,
        tool: str,
        arguments: dict[str, Any],
        context: ToolContext,
        callbacks: ChatCallbacks,
        reply: AssistantReply,
    ) -> bool:
        call = ToolCall(tool, arguments, source="deterministic")
        callbacks.tool_proposed(call)
        callbacks.tool_started(tool, arguments)
        outcome = self.executor.execute(call, context, confirmation_provider=context.confirm)
        callbacks.tool_finished(outcome)
        reply.tool_runs.append(_tool_run(tool, arguments, outcome))
        reply.deterministic = True
        if outcome.status == "declined":
            reply.text = outcome.result.summary or "No problem - I did not change anything."
        else:
            reply.text = outcome.result.summary or outcome.message
        return True

    def _handle_pending_answer(
        self,
        pending: PendingAction,
        message: str,
        context: ToolContext,
        callbacks: ChatCallbacks,
        reply: AssistantReply,
    ) -> bool:
        if _NEGATIVE.match(message):
            reply.text = "Understood - I did not save it."
            reply.deterministic = True
            return True
        if not _AFFIRM.match(message):
            return False

        payload = dict(pending.payload or {})
        detail = _extract_detail(payload)
        if _CORRECTION.search(message):
            # "yes, but make it 7pm instead": re-read the correction and keep the
            # rest of what we already agreed on.
            corrected = nlp.parse_task_text(f"{pending.text} {message}")
            detail = _merge_detail(detail, corrected)
        if not detail.get("title"):
            reply.text = "I lost track of what to save there - could you say it once more?"
            reply.deterministic = True
            return True
        return self._save_confirmed(pending.kind == "reminder", detail, context, callbacks, reply)

    def _save_confirmed(
        self,
        is_reminder: bool,
        detail: dict[str, Any],
        context: ToolContext,
        callbacks: ChatCallbacks,
        reply: AssistantReply,
    ) -> bool:
        """Save exactly what the user just confirmed (no re-parsing surprises)."""
        when = detail.get("remind_at") or detail.get("due_at")
        if is_reminder:
            arguments: dict[str, Any] = {"title": detail["title"]}
            if when:
                arguments["when"] = to_iso(when)
            if detail.get("recurrence") and detail["recurrence"] != "none":
                arguments["recurrence"] = detail["recurrence"]
            return self._run_tool_text("set_reminder", arguments, context, callbacks, reply)

        arguments = {"title": detail["title"]}
        if detail.get("due_at"):
            arguments["due"] = to_iso(detail["due_at"])
        if detail.get("priority"):
            arguments["priority"] = detail["priority"]
        if detail.get("category") and detail["category"] != "general":
            arguments["category"] = detail["category"]
        if detail.get("recurrence") and detail["recurrence"] != "none":
            arguments["recurrence"] = detail["recurrence"]
        if detail.get("tags"):
            arguments["tags"] = list(detail["tags"])
        if detail.get("remind_at"):
            from lumi.utils.timeutil import from_iso

            due = from_iso(arguments.get("due")) or detail["remind_at"]
            minutes = int((due - detail["remind_at"]).total_seconds() // 60)
            if minutes > 0:
                arguments["remind_minutes_before"] = minutes
        return self._run_tool_text("add_task", arguments, context, callbacks, reply)

    # --------------------------------------------------------------- the model
    def _ask_model(
        self,
        message: str,
        conversation_id: Optional[int],
        callbacks: ChatCallbacks,
        stop_token: StopToken,
        context: ToolContext,
        reply: AssistantReply,
        allow_tools: bool,
    ) -> _ModelAnswer:
        model = self.selected_model()
        if not model:
            names = self.model_names()
            if len(names) == 1:
                model = names[0]
            else:
                return _ModelAnswer(
                    error="No local model is selected yet.",
                    hint=(
                        "Open Settings → Models and install one (for example 'llama3.2:3b'), then select it. "
                        "Tasks, notes, reminders and file search work without a model."
                    ),
                )

        messages = self._build_messages(message, conversation_id, context, allow_tools, model)
        provider = reply.provider or self._choose_provider()
        use_native = allow_tools and provider == "local" and self._native_tools_available(model)
        tools_payload = (
            self.registry.schemas_for_model(context, self.executor.policy) if use_native else None
        )

        turns: list[str] = []
        metrics: dict[str, Any] = {}
        rounds = 0

        while True:
            rounds += 1
            hint = f"Thinking with {model}…" if rounds == 1 else "Reading the tool result…"
            callbacks.status(hint)
            callbacks.message_start(model)
            text_parts: list[str] = []
            native_calls: list[dict[str, Any]] = []
            try:
                for chunk in self._stream(provider, messages, model, tools_payload, stop_token):
                    if stop_token.cancelled:
                        reply.stopped = True
                        break
                    if chunk.thinking:
                        callbacks.reasoning(chunk.thinking)
                    if chunk.text:
                        text_parts.append(chunk.text)
                        callbacks.token(chunk.text)
                    if chunk.tool_calls:
                        native_calls.extend(chunk.tool_calls)
                    if chunk.metrics:
                        metrics.update(chunk.metrics)
            except OllamaError as exc:
                partial = "".join(text_parts).strip()
                if partial:
                    turns.append(partial)
                    break
                return _ModelAnswer(text="".join(turns), error=exc.message, hint=exc.hint, stopped=reply.stopped, metrics=metrics)

            text = "".join(text_parts)
            if text.strip():
                turns.append(text)

            parsed = toolcalls.ParseResult(calls=toolcalls.normalise_native_calls(native_calls) if native_calls else [], text=text)
            if not parsed.calls and allow_tools and rounds <= self._max_tool_rounds():
                parsed = toolcalls.parse_tool_calls(text, self.registry.names())

            if not parsed.calls or stop_token.cancelled or rounds > self._max_tool_rounds():
                break

            call = parsed.calls[0]
            if len(parsed.calls) > 1:
                log.info("model proposed %s tool calls; using the first", len(parsed.calls))
            outcome = self._run_model_tool(call, context, callbacks, reply)

            if use_native:
                messages.append(
                    {
                        "role": "assistant",
                        "content": text,
                        "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
                    }
                )
            else:
                messages.append({"role": "assistant", "content": text})
            messages.append(build_tool_result_message(call.name, outcome.result.to_payload(), native=use_native))

        final = "\n\n".join(part for part in turns if part.strip()).strip()
        if not final and reply.tool_runs:
            final = reply.tool_runs[-1].get("summary", "")
        return _ModelAnswer(text=final, stopped=reply.stopped, metrics=metrics)

    def _stream(
        self,
        provider: str,
        messages: list[dict[str, Any]],
        model: str,
        tools_payload: Any,
        stop_token: StopToken,
    ) -> Iterator[ChatChunk]:
        settings = self.settings
        options = {
            "temperature": settings.generation.temperature,
            "num_ctx": settings.generation.num_ctx,
            "num_predict": settings.generation.num_predict,
            "top_p": settings.generation.top_p,
            "top_k": settings.generation.top_k,
            "repeat_penalty": settings.generation.repeat_penalty,
        }
        if settings.generation.seed is not None:
            options["seed"] = settings.generation.seed
        if provider == "local":
            yield from self.client().chat(
                messages,
                model=model,
                options=options,
                tools=tools_payload,
                stream=True,
                keep_alive=settings.ollama.keep_alive,
                stop_token=stop_token,
                read_timeout_s=settings.ollama.request_timeout_s,
            )
            return
        implementation = self._providers.get(provider)
        if implementation is None:
            raise OllamaError(f"The {provider} provider is not configured.", kind="http")
        yield from implementation.chat(messages, model=model, options=options, tools=tools_payload, stop_token=stop_token)

    def _run_model_tool(
        self,
        call: toolcalls.ParsedCall,
        context: ToolContext,
        callbacks: ChatCallbacks,
        reply: AssistantReply,
    ) -> ExecutionOutcome:
        tool_call = ToolCall(call.name, dict(call.arguments), source="model", raw=call.raw)
        callbacks.tool_proposed(tool_call)
        callbacks.tool_started(call.name, tool_call.arguments)
        callbacks.status(f"Using {call.name}…")
        outcome = self.executor.execute(tool_call, context, confirmation_provider=context.confirm)
        callbacks.tool_finished(outcome)
        reply.tool_runs.append(_tool_run(call.name, tool_call.arguments, outcome))
        return outcome

    def _max_tool_rounds(self) -> int:
        return max(1, int(getattr(self.settings.model, "max_tool_iterations", MAX_TOOL_ROUNDS) or MAX_TOOL_ROUNDS))

    def _native_tools_available(self, model: str) -> bool:
        if not self.tools_enabled():
            return False
        return self.supports_tools(model)

    def _choose_provider(self) -> str:
        """Local unless the user deliberately configured and enabled their own key."""
        cloud = self.settings.cloud
        preferred = getattr(cloud, "preferred_provider", "local") or "local"
        if preferred in {"", "local"}:
            return "local"
        implementation = self._providers.get(preferred)
        if implementation is None:
            return "local"
        try:
            available, _reason = implementation.available()
        except Exception:  # pragma: no cover - provider bug must not break chat
            return "local"
        return preferred if available else "local"

    # ----------------------------------------------------------- prompt build
    def _build_messages(
        self,
        message: str,
        conversation_id: Optional[int],
        context: ToolContext,
        allow_tools: bool,
        model: str,
    ) -> list[dict[str, Any]]:
        memory_lines: list[str] = []
        try:
            memory_lines = format_memory_lines(self.memory.relevant_for_prompt(message, limit=MEMORY_LIMIT))
        except Exception:  # pragma: no cover - memory is a bonus
            log.debug("memory lookup failed", exc_info=True)

        tool_lines: list[str] = []
        use_native = allow_tools and self._native_tools_available(model)
        if allow_tools:
            try:
                tool_lines = self.registry.docs_for_prompt(context, self.executor.policy)
            except Exception:  # pragma: no cover
                tool_lines = []
        elif self.settings.model.enable_tools is False:
            tool_lines = []

        system_prompt = build_system_prompt(
            settings=self.settings,
            hardware_summary=str(context.services.get("hardware_summary", "") or ""),
            memory_lines=memory_lines,
            tool_lines=tool_lines,
            workspace=str(context.services.get("workspace_summary", "") or ""),
            tool_calling="native" if use_native else "json",
            local_only=self._choose_provider() == "local",
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend(self._history(conversation_id))
        messages.append({"role": "user", "content": message})
        return messages

    def _history(self, conversation_id: Optional[int]) -> list[dict[str, Any]]:
        if not conversation_id or not self.settings.privacy.save_conversations:
            return []
        try:
            stored = self.conversations.messages(int(conversation_id), limit=HISTORY_MESSAGES + 1)
        except Exception:  # pragma: no cover
            return []
        if stored and stored[-1].role == "user":
            stored = stored[:-1]  # the current message was stored a moment ago
        total = 0
        trimmed: list[dict[str, Any]] = []
        for entry in reversed(stored):
            content = (entry.content or "").strip()
            if not content:
                continue
            total += len(content)
            if total > HISTORY_CHARS:
                break
            trimmed.append({"role": entry.role if entry.role in {"user", "assistant"} else "assistant", "content": content})
        trimmed.reverse()
        return trimmed

    # -------------------------------------------------------------- history
    def _store_reply(self, conversation_id: Optional[int], reply: AssistantReply, save_history: bool) -> None:
        if not save_history or not conversation_id or not self.settings.privacy.save_conversations:
            return
        text = reply.text.strip()
        if not text:
            return
        try:
            meta: dict[str, Any] = {}
            if reply.tool_runs:
                meta["tools"] = [run.get("tool") for run in reply.tool_runs]
            if reply.model:
                meta["model"] = reply.model
            self.conversations.add_message(
                int(conversation_id),
                "assistant",
                text,
                model=reply.model,
                provider=reply.provider,
                meta=meta or None,
            )
        except Exception:  # pragma: no cover - history is best effort
            log.debug("could not store the assistant reply", exc_info=True)

    # ------------------------------------------------------------ helpers
    def pending_question(self, conversation_id: Optional[int]) -> str:
        pending = self._pending.get(int(conversation_id or 0))
        return pending.question if pending else ""

    def clear_pending(self, conversation_id: Optional[int] = None) -> None:
        if conversation_id is None:
            self._pending.clear()
        else:
            self._pending.pop(int(conversation_id), None)

    def describe_state(self) -> dict[str, Any]:
        status = self.status()
        return {
            "ollama": status.to_dict(),
            "selected_model": self.selected_model(),
            "provider": self._choose_provider(),
            "tools": self.registry.stats(),
            "deterministic_commands": bool(self.settings.productivity.deterministic_commands),
        }


def _tool_run(tool: str, arguments: dict[str, Any], outcome: ExecutionOutcome) -> dict[str, Any]:
    return {
        "tool": tool,
        "arguments": arguments,
        "status": outcome.status,
        "summary": outcome.result.summary,
        "error": outcome.result.error,
        "verified": outcome.result.verified,
        "duration_ms": outcome.duration_ms,
    }


def _extract_detail(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a stored parse result back into comparable datetimes."""
    from lumi.utils.timeutil import from_iso

    detail: dict[str, Any] = {
        "title": str(payload.get("title") or "").strip(),
        "priority": payload.get("priority") or "",
        "category": payload.get("category") or "",
        "recurrence": payload.get("recurrence") or "none",
        "tags": list(payload.get("tags") or []),
    }
    due = from_iso(payload.get("due_at")) if payload.get("due_at") else None
    remind = from_iso(payload.get("remind_at")) if payload.get("remind_at") else None
    if due:
        detail["due_at"] = due
    if remind:
        detail["remind_at"] = remind
    return detail


def _merge_detail(detail: dict[str, Any], parsed: Any) -> dict[str, Any]:
    """Apply a correction sentence on top of an already-confirmed intent."""
    merged = dict(detail)
    if getattr(parsed, "title", ""):
        merged["title"] = parsed.title
    if getattr(parsed, "due_at", None):
        merged["due_at"] = parsed.due_at
    if getattr(parsed, "remind_at", None):
        merged["remind_at"] = parsed.remind_at
    if getattr(parsed, "recurrence", "none") not in {"", "none"}:
        merged["recurrence"] = parsed.recurrence
    if getattr(parsed, "priority", None):
        merged["priority"] = parsed.priority
    if getattr(parsed, "tags", None):
        merged["tags"] = list(parsed.tags)
    return merged


def _mentions_reminder(text: str) -> bool:
    return bool(re.search(r"\bremind(er|ers)?\b", text or "", re.I))


def _remember_suggestion(message: str) -> str:
    """Pull out the sentence a user asked LUMI to remember, if any."""
    match = _REMEMBER_HINT.search(message)
    if not match:
        return ""
    sentence = message[match.start() :].strip()
    sentence = re.split(r"(?<=[.!?])\s+", sentence)[0].strip()
    cleaned = re.sub(
        r"^(please\s+)?(remember|note down|keep in mind|don'?t forget)\s+(that\s+|this\s*:?\s*)?",
        "",
        sentence,
        flags=re.I,
    ).strip()
    if len(cleaned) < 6 or len(cleaned) > 300:
        return ""
    return cleaned
