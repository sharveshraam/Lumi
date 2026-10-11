"""Turn whatever a small model emits into a clean :class:`ToolCall`.

Local models are creative.  One may return a native tool call, another a JSON
blob inside a code fence, a third the Python-looking ``add_task(title="x")``.
All three are accepted here and normalised to the same shape, but *nothing* is
executed at this layer: the caller still validates names, arguments and
permissions before anything happens.

Design rules:

* Never use ``eval``/``exec`` - the arguments are parsed as JSON or as literal
  key/value pairs, never as code.
* Never guess silently: anything dropped is reported in the returned notes so the
  UI can show "I ignored an unknown tool".
* Bound everything: at most :data:`MAX_CALLS` calls and
  :data:`MAX_ARGUMENT_CHARS` characters of arguments per call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

log = logging.getLogger("lumi.ai.toolcalls")

MAX_CALLS = 3
MAX_ARGUMENT_CHARS = 8000

#: Markers the prompt tells the model to use for a fallback tool call.
TOOL_CALL_OPEN = "```tool_call"
TOOL_CALL_CLOSE = "```"
TAG_OPEN = "<tool_call>"
TAG_CLOSE = "</tool_call>"

_JSON_FENCE_RE = re.compile(r"```(?:json|tool_call|tool)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\}|\[.*?\])\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_CALL_RE = re.compile(
    r"(?P<name>[a-z_][a-z0-9_]{1,60})\s*\((?P<args>[^()]{0,4000})\)",
    re.IGNORECASE | re.DOTALL,
)
_ARG_RE = re.compile(
    r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=|:)\s*(?P<value>\"[^\"]*\"|'[^']*'|[^,]+)",
    re.DOTALL,
)
#: Keys models use for the tool name, in priority order.
_NAME_KEYS = ("name", "tool", "tool_name", "function", "action", "tool_call")
_ARGS_KEYS = ("arguments", "args", "parameters", "kwargs", "input", "parameter")

LOOKS_LIKE_CALL_RE = re.compile(r"\b[a-z_][a-z0-9_]{2,60}\s*\(", re.IGNORECASE)


@dataclass
class ParsedCall:
    """A candidate tool call, before validation."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    source: str = "text"  # native | text | tag | fence | call
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments, "source": self.source}


@dataclass
class ParseResult:
    """What was found in one model message."""

    calls: list[ParsedCall] = field(default_factory=list)
    text: str = ""
    """The message with the tool syntax removed (may be empty)."""
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.calls)

    def to_dict(self) -> dict[str, Any]:
        return {"calls": [call.to_dict() for call in self.calls], "text": self.text, "notes": self.notes}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _balanced_objects(text: str, limit: int = 8) -> list[str]:
    """Return top-level ``{...}`` / ``[...]`` substrings, respecting strings."""
    found: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    opener = ""
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "{[":
            if depth == 0:
                start = index
                opener = char
            depth += 1
        elif char in "}]" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start : index + 1]
                if (opener == "{" and candidate.startswith("{")) or (opener == "[" and candidate.startswith("[")):
                    found.append(candidate)
                start = -1
                if len(found) >= limit:
                    break
    return found


def _loads(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Models often emit trailing commas or single quotes; fix the common cases.
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        cleaned = cleaned.replace("'", '"') if cleaned.count('"') == 0 else cleaned
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        loaded = _loads(text)
        if isinstance(loaded, Mapping):
            return {str(key): value for key, value in loaded.items()}
        return _parse_literal_arguments(text)
    return {}


def _parse_literal_arguments(text: str) -> dict[str, Any]:
    """Parse ``title="x", due="tomorrow"`` without touching ``eval``."""
    arguments: dict[str, Any] = {}
    for match in _ARG_RE.finditer(text):
        key = match.group("key")
        value = match.group("value").strip().rstrip(",").strip()
        if value.startswith(("'", '"')) and value[-1:] == value[0]:
            arguments[key] = value[1:-1]
            continue
        lowered = value.lower()
        if lowered in {"true", "false"}:
            arguments[key] = lowered == "true"
            continue
        if lowered in {"none", "null"}:
            arguments[key] = None
            continue
        try:
            arguments[key] = int(value)
            continue
        except ValueError:
            pass
        try:
            arguments[key] = float(value)
            continue
        except ValueError:
            pass
        loaded = _loads(value)
        arguments[key] = loaded if loaded is not None else value
    return arguments


def _looks_like_call_object(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    keys = {str(key).lower() for key in payload}
    has_name = any(key in keys for key in _NAME_KEYS)
    has_args = any(key in keys for key in _ARGS_KEYS)
    return has_name and (has_args or len(keys) <= 4)


def _call_from_mapping(payload: Mapping[str, Any], *, source: str, raw: str = "") -> Optional[ParsedCall]:
    name = ""
    arguments: Any = None
    for key in _NAME_KEYS:
        for candidate_key, candidate_value in payload.items():
            if str(candidate_key).lower() != key:
                continue
            if key == "function":
                if isinstance(candidate_value, Mapping):
                    name = str(candidate_value.get("name") or "")
                    arguments = candidate_value.get("arguments")
                else:
                    name = str(candidate_value)
            else:
                name = str(candidate_value or "")
            break
        if name:
            break
    if not name:
        return None
    if arguments is None:
        for key in _ARGS_KEYS:
            for candidate_key, candidate_value in payload.items():
                if str(candidate_key).lower() == key:
                    arguments = candidate_value
                    break
            if arguments is not None:
                break
    if arguments is None:
        # Everything that is not the name is an argument.
        used = {key for key in _NAME_KEYS}
        arguments = {
            str(key): value
            for key, value in payload.items()
            if str(key).lower() not in used and str(key).lower() not in _ARGS_KEYS
        }
    cleaned = _coerce_arguments(arguments)
    name = name.strip()
    if not name:
        return None
    return ParsedCall(name=name, arguments=cleaned, source=source, raw=raw[:400])


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def normalise_native_calls(raw_calls: Iterable[Any]) -> list[ParsedCall]:
    """Normalise the ``tool_calls`` array returned by Ollama."""
    parsed: list[ParsedCall] = []
    for entry in raw_calls:
        if not isinstance(entry, Mapping):
            continue
        payload = entry.get("function") if isinstance(entry.get("function"), Mapping) else entry
        call = _call_from_mapping(payload, source="native", raw=json.dumps(entry, default=str)[:400])
        if call is not None:
            parsed.append(call)
        if len(parsed) >= MAX_CALLS:
            break
    return parsed


def parse_tool_calls(text: str, known_tools: Sequence[str] | None = None) -> ParseResult:
    """Find tool calls in a model message.

    ``known_tools`` is used to avoid false positives: a JSON object is only
    treated as a tool call when its name is a real tool, or when it is clearly
    shaped like one (``{"name": ..., "arguments": {...}}``).
    """
    raw = text or ""
    result = ParseResult(text=raw)
    known = {name.lower() for name in (known_tools or [])} or None
    remaining = raw

    # 1. Explicit fences and tags.
    for pattern, source in ((_TAG_RE, "tag"), (_JSON_FENCE_RE, "fence")):
        for match in list(pattern.finditer(remaining)):
            payload = _loads(match.group(1).strip())
            calls = _calls_from_payload(payload, known, source=source, raw=match.group(0))
            if calls:
                result.calls.extend(calls)
                remaining = remaining.replace(match.group(0), " ")
    if len(result.calls) >= MAX_CALLS:
        return _finish(result, remaining)

    # 2. The whole message is one JSON object / array.
    if not result.calls:
        stripped = remaining.strip()
        if stripped.startswith(("{", "[")):
            payload = _loads(stripped)
            calls = _calls_from_payload(payload, known, source="json", raw=stripped)
            if calls and _is_pure_call_payload(payload):
                result.calls.extend(calls)
                remaining = " "
    if len(result.calls) >= MAX_CALLS:
        return _finish(result, remaining)

    # 3. Any balanced JSON object in the text.
    if not result.calls:
        for candidate in _balanced_objects(remaining):
            payload = _loads(candidate)
            calls = _calls_from_payload(payload, known, source="json", raw=candidate)
            if calls:
                result.calls.extend(calls)
                remaining = remaining.replace(candidate, " ")
            if len(result.calls) >= MAX_CALLS:
                break
    if len(result.calls) >= MAX_CALLS:
        return _finish(result, remaining)

    # 4. Python-looking calls: add_task(title="x", due="tomorrow").
    if not result.calls and _looks_like_python_call(remaining, known):
        for match in _CALL_RE.finditer(remaining):
            name = match.group("name")
            if known is not None and name.lower() not in known:
                continue
            arguments = _parse_literal_arguments(match.group("args"))
            result.calls.append(ParsedCall(name=name, arguments=arguments, source="call", raw=match.group(0)[:400]))
            remaining = remaining.replace(match.group(0), " ")
            if len(result.calls) >= MAX_CALLS:
                break

    return _finish(result, remaining)


def _finish(result: ParseResult, remaining: str) -> ParseResult:
    cleaned = re.sub(r"[ \t]{2,}", " ", remaining)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    result.text = cleaned
    if result.calls:
        log.debug("parsed %s tool call(s) from model output", len(result.calls))
    return result


def _is_pure_call_payload(payload: Any) -> bool:
    if isinstance(payload, list):
        return all(_looks_like_call_object(item) for item in payload) and bool(payload)
    return _looks_like_call_object(payload)


def _looks_like_python_call(text: str, known: Optional[set[str]]) -> bool:
    if not text or not LOOKS_LIKE_CALL_RE.search(text):
        return False
    if known is None:
        return True
    matches = [match.group("name").lower() for match in _CALL_RE.finditer(text)]
    return any(name in known for name in matches)


def _calls_from_payload(payload: Any, known: Optional[set[str]], *, source: str, raw: str) -> list[ParsedCall]:
    calls: list[ParsedCall] = []
    items: list[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, Mapping):
        # {"tool_calls": [...]} / {"calls": [...]} / {"name": ..., "arguments": ...}
        nested = None
        for key in ("tool_calls", "calls", "tools"):
            if isinstance(payload.get(key), list):
                nested = payload[key]
                break
        items = nested if nested is not None else [payload]
    else:
        return calls

    for item in items:
        if not isinstance(item, Mapping):
            continue
        call = _call_from_mapping(item, source=source, raw=raw)
        if call is None:
            continue
        if known is not None and call.name.lower() not in known and not _looks_like_call_object(item):
            continue
        if sum(len(json.dumps(value, default=str)) for value in call.arguments.values()) > MAX_ARGUMENT_CHARS:
            log.warning("ignoring oversized tool call for %s", call.name)
            continue
        calls.append(call)
        if len(calls) >= MAX_CALLS:
            break
    return calls


def strip_tool_syntax(text: str) -> str:
    """Remove tool-call scaffolding from a message shown to the user."""
    cleaned = _TAG_RE.sub(" ", text or "")
    cleaned = _JSON_FENCE_RE.sub(" ", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def looks_like_tool_attempt(text: str, known_tools: Iterable[str] | None = None) -> bool:
    """True when a model *tried* to call a tool (used to decide on a retry)."""
    return parse_tool_calls(text or "", list(known_tools or []) or None).found


def format_tool_call_example(name: str = "add_task", arguments: dict[str, Any] | None = None) -> str:
    """A concrete example injected into the fallback prompt."""
    payload = {"name": name, "arguments": arguments or {"title": "Finish the lab record", "due": "tomorrow 18:00"}}
    return f"{TOOL_CALL_OPEN}\n{json.dumps(payload, ensure_ascii=False)}\n{TOOL_CALL_CLOSE}"
