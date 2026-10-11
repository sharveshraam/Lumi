"""Strict argument validation.

A model - or a user typing JSON by hand - is untrusted input.  Every tool
argument is validated against the tool's JSON schema before anything happens.

Two backends:

* :mod:`jsonschema` when installed (full draft support, better messages), and
* a small built-in validator that covers the subset of JSON Schema the tools
  actually use.  The built-in one is always used for the *planner* JSON envelope
  so behaviour is identical on every machine, and it keeps LUMI functional if
  the optional dependency is missing.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Sequence

try:  # pragma: no cover - optional dependency
    import jsonschema as _jsonschema
except Exception:  # pragma: no cover
    _jsonschema = None

MAX_STRING_LENGTH = 20000
MAX_ARRAY_ITEMS = 500


class ValidationError(Exception):
    """Raised for malformed argument payloads."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) if self.errors else "invalid arguments")


def validate_arguments(schema: dict[str, Any] | None, arguments: Any, *, backend: str = "auto") -> list[str]:
    """Return a list of human-readable problems (empty list means valid)."""
    if schema is None:
        schema = {"type": "object", "properties": {}}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return [f"arguments must be a JSON object, got {type(arguments).__name__}"]

    errors: list[str] = []
    use_jsonschema = backend == "jsonschema" or (backend == "auto" and _jsonschema is not None)
    if use_jsonschema and _jsonschema is not None:
        try:
            validator_cls = _jsonschema.validators.validator_for(schema, default=_jsonschema.Draft202012Validator)
            validator = validator_cls(schema)
            for error in sorted(validator.iter_errors(arguments), key=lambda item: list(item.absolute_path)):
                location = ".".join(str(part) for part in error.absolute_path) or "(root)"
                errors.append(f"{location}: {error.message}")
            if errors:
                return errors
        except Exception:  # pragma: no cover - schema problem, fall through to builtin
            errors = []

    return _validate_builtin(schema, arguments) if not errors else errors


def validate_or_raise(schema: dict[str, Any] | None, arguments: Any, *, backend: str = "auto") -> dict[str, Any]:
    errors = validate_arguments(schema, arguments, backend=backend)
    if errors:
        raise ValidationError(errors)
    return dict(arguments or {})


# ---------------------------------------------------------------------------
# built-in validator
# ---------------------------------------------------------------------------


def _validate_builtin(schema: dict[str, Any], value: Any, path: str = "") -> list[str]:
    errors: list[str] = []
    location = path or "(root)"

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, item) for item in types):
            return [f"{location}: expected {', '.join(str(item) for item in types)}, got {_type_name(value)}"]

    if "enum" in schema and value not in schema["enum"]:
        return [f"{location}: must be one of {', '.join(repr(item) for item in schema['enum'])}"]

    if isinstance(value, str):
        limit = int(schema.get("maxLength") or MAX_STRING_LENGTH)
        if len(value) > limit:
            errors.append(f"{location}: text is too long (limit {limit} characters)")
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{location}: text must be at least {schema['minLength']} characters")
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            errors.append(f"{location}: does not match the required format")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{location}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{location}: must be <= {schema['maximum']}")
        if "multipleOf" in schema and schema["multipleOf"]:
            quotient = value / schema["multipleOf"]
            if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                errors.append(f"{location}: must be a multiple of {schema['multipleOf']}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{location}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{location}: allows at most {schema['maxItems']} item(s)")
        if len(value) > MAX_ARRAY_ITEMS:
            errors.append(f"{location}: too many items (limit {MAX_ARRAY_ITEMS})")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_builtin(item_schema, item, f"{location}[{index}]"))
    elif isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        additional = schema.get("additionalProperties", False)
        for name in required:
            if name not in value:
                errors.append(f"{location}: missing required argument '{name}'")
        if additional is False:
            unknown = [name for name in value if name not in properties]
            if unknown:
                errors.append(f"{location}: unexpected argument(s): {', '.join(sorted(unknown))}")
        for name, item in value.items():
            if name in properties:
                errors.extend(_validate_builtin(properties[name], item, f"{path}.{name}" if path else name))
    return errors


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    if expected == "any":
        return True
    return True


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def coerce_arguments(arguments: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce obvious string/number mismatches produced by smaller models.

    Only *safe*, unambiguous conversions are performed: ``"true"`` -> ``True``,
    ``"5"`` -> ``5`` for integer fields, JSON-encoded arrays/objects decoded.
    Anything else is left untouched so validation can report it.
    """
    if not schema or not isinstance(arguments, dict):
        return dict(arguments or {})
    properties = schema.get("properties") or {}
    result: dict[str, Any] = {}
    for name, value in arguments.items():
        spec = properties.get(name) or {}
        expected = spec.get("type")
        if isinstance(value, str):
            stripped = value.strip()
            if expected == "boolean" and stripped.lower() in {"true", "false"}:
                result[name] = stripped.lower() == "true"
                continue
            if expected == "integer" and re.fullmatch(r"-?\d+", stripped):
                result[name] = int(stripped)
                continue
            if expected == "number" and re.fullmatch(r"-?\d+(\.\d+)?", stripped):
                result[name] = float(stripped)
                continue
            if expected == "array" and stripped.startswith("["):
                try:
                    decoded = json.loads(stripped)
                except ValueError:
                    decoded = None
                if isinstance(decoded, list):
                    result[name] = decoded
                    continue
            if expected == "object" and stripped.startswith("{"):
                try:
                    decoded = json.loads(stripped)
                except ValueError:
                    decoded = None
                if isinstance(decoded, dict):
                    result[name] = decoded
                    continue
            if expected is None:
                result[name] = value
                continue
        result[name] = value
    return result


def summarise_errors(errors: Iterable[str], *, limit: int = 3) -> str:
    items = list(errors)[:limit]
    return "; ".join(items)


def sanitise_string(value: str, *, max_length: int = MAX_STRING_LENGTH, strip_control: bool = True) -> str:
    """Remove control characters and cap length (defence against odd payloads)."""
    text = value[:max_length]
    if strip_control:
        text = "".join(char for char in text if char == "\n" or char == "\t" or ord(char) >= 32)
    return text
