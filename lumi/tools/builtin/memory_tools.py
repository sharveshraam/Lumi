"""Memory tools.

The assistant may only write long-term memory when the user asked for it.  The
tool descriptions say so explicitly, the policy blocks sensitive content, and the
UI always shows what was stored so it can be edited or deleted.
"""

from __future__ import annotations

from typing import Any, Iterable

from lumi.memory.models import KIND_SESSION, KIND_FACT, KIND_PREFERENCE, KIND_PROJECT, MEMORY_KINDS
from lumi.security.redact import redact_text
from lumi.tools.base import ConfirmationRequest, RiskLevel, Tool, ToolContext, ToolError, ToolResult
from lumi.tools.builtin._common import (
    SERVICE_MEMORY,
    bool_arg,
    int_arg,
    ok,
    service,
    string_list,
)

WRITABLE_KINDS = [KIND_PREFERENCE, KIND_FACT, KIND_PROJECT]


class Remember(Tool):
    name = "remember"
    description = (
        "Save a long-term fact or preference to local memory. Only use this when the user asks you "
        "to remember something (or agrees after you asked). Never store passwords, tokens, card or "
        "ID numbers - the tool refuses those automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "maxLength": 2000, "description": "The fact, phrased in the third person, e.g. 'submits lab records on Fridays'."},
            "kind": {
                "type": "string",
                "enum": WRITABLE_KINDS,
                "description": "preference = how LUMI should behave; fact = something durable about the user; project = context for one project or course.",
            },
            "key": {"type": "string", "maxLength": 80, "description": "Optional short label so the entry can be updated later (e.g. 'preferred_name')."},
            "replace": {"type": "boolean", "description": "Replace any existing entry with the same key."},
        },
        "required": ["content"],
    }
    category = "memory"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0
    examples = ("remember that I prefer short answers", "remember I use Python 3.11")

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        memory = service(context, SERVICE_MEMORY)
        content = str(arguments.get("content", "")).strip()
        if not content:
            raise ToolError("There was nothing to remember.")
        kind = str(arguments.get("kind") or KIND_FACT)
        if kind not in MEMORY_KINDS or kind == "session":
            kind = KIND_FACT
        key = str(arguments.get("key") or "").strip()
        replace = bool_arg(arguments, "replace", True)
        try:
            if key and replace:
                entry = memory.upsert_by_key(key, content, kind=kind, source="chat")
            else:
                entry = memory.add(content, kind=kind, key=key, source="chat")
        except ValueError as exc:
            raise ToolError(str(exc), hint="I did not save it. Tell me a different way if you want it kept.") from exc
        return ok(
            f"Saved to memory ({entry.kind}): {redact_text(entry.content, max_length=200)}",
            data={"memory": entry.to_dict()},
            verified=True,
        )


class Recall(Tool):
    name = "recall"
    description = (
        "Look up saved memories. Use it before claiming to remember something about the user. "
        "If nothing matches, say so instead of guessing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for. Leave empty to list everything."},
            "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    }
    category = "memory"
    risk = RiskLevel.SAFE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        memory = service(context, SERVICE_MEMORY)
        query = str(arguments.get("query", "")).strip()
        kind = str(arguments.get("kind") or "").strip() or None
        limit = int_arg(arguments, "limit", 20)
        entries = (
            memory.search(query, kinds=[kind] if kind else None, limit=limit)
            if query
            else memory.list(kind=kind, limit=limit)
        )
        entries = [entry for entry in entries if entry.kind != KIND_SESSION]
        if not entries:
            return ok(
                "Nothing is stored about that. I will ask rather than guess."
                if query
                else "Memory is empty.",
                data={"memories": []},
                verified=True,
            )
        for entry in entries:
            if entry.id is not None:
                try:
                    memory.touch(int(entry.id))
                except Exception:  # pragma: no cover - statistics only
                    pass
        lines = [f"#{entry.id or 'session'} [{entry.kind}] {redact_text(entry.content, max_length=300)}" for entry in entries]
        return ok(
            "\n".join(lines),
            data={"memories": [entry.to_dict() for entry in entries]},
            verified=True,
        )


class ForgetMemory(Tool):
    name = "forget_memory"
    description = "Delete one saved memory. Asks for confirmation first."
    parameters = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "integer", "description": "The #number shown by 'recall'."},
            "query": {"type": "string", "description": "Text of the memory, if the number is unknown."},
        },
    }
    category = "memory"
    risk = RiskLevel.CONSEQUENTIAL
    always_confirm = True
    confirmation_title = "Forget this memory?"
    permission_action = "delete_memory"
    timeout_s = 10.0

    def _find(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        memory = service(context, SERVICE_MEMORY)
        memory_id = arguments.get("memory_id")
        if memory_id not in (None, "", 0):
            entry = memory.get(int(memory_id))
            if entry is None:
                raise ToolError(f"There is no memory #{memory_id}.")
            return entry
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ToolError("Tell me which memory to forget.")
        matches = [entry for entry in memory.search(query, limit=5) if entry.id is not None]
        if not matches:
            raise ToolError(f"I found no memory matching '{query}'.")
        if len(matches) > 1:
            options = "; ".join(f"#{entry.id} {redact_text(entry.content, max_length=60)}" for entry in matches)
            raise ToolError(f"That matches several memories: {options}. Give me the #number.")
        return matches[0]

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        request = super().confirmation_details(arguments, context)
        try:
            entry = self._find(arguments, context)
            request.details = f"Memory #{entry.id} ({entry.kind})"
            request.preview = redact_text(entry.content, max_length=300)
        except ToolError:
            request.details = "Delete the requested memory."
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        memory = service(context, SERVICE_MEMORY)
        entry = self._find(arguments, context)
        if not memory.delete(int(entry.id)):
            raise ToolError(f"Memory #{entry.id} was already gone.")
        return ok(
            f"Forgotten: {redact_text(entry.content, max_length=120)}",
            data={"memory_id": entry.id},
            verified=True,
        )


class ListMemories(Tool):
    name = "list_memories"
    description = "Overview of what LUMI has stored long term, grouped by kind."
    parameters = {"type": "object", "properties": {}}
    category = "memory"
    risk = RiskLevel.SAFE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        memory = service(context, SERVICE_MEMORY)
        counts = memory.counts_by_kind()
        # Session notes live in RAM only; they are not "stored" and must not be
        # presented as if they were.
        entries = [entry for entry in memory.list(limit=40) if entry.kind != KIND_SESSION]
        lines = [f"#{entry.id} [{entry.kind}] {redact_text(entry.content, max_length=140)}" for entry in entries]
        summary = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items())) or "nothing stored yet"
        return ok(
            f"Memory contains {summary}.\n\n" + ("\n".join(lines) if lines else ""),
            data={"counts": counts, "memories": [entry.to_dict() for entry in entries]},
            verified=True,
        )


def build_tools() -> Iterable[Tool]:
    return (Remember(), Recall(), ForgetMemory(), ListMemories())
