"""Redaction helpers.

Every log line, audit record and error string that LUMI writes goes through
:func:`redact_text`.  The goal is *minimisation*: a log file should never be a
place where an API key, a password or the body of an email ends up by accident.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: Substrings that mark a key name as sensitive (case-insensitive).
SENSITIVE_KEY_HINTS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "cookie",
    "session",
    "auth",
    "bearer",
    "otp",
    "pin",
)

_PLACEHOLDER = "***redacted***"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Provider API keys / tokens that have recognisable shapes.
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"), "***redacted-google-key***"),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{16,}\b"), "***redacted-openai-key***"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{16,}\b"), "***redacted-anthropic-key***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "***redacted-github-token***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "***redacted-slack-token***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b"), "***redacted-jwt***"),
    # key=value / "key": "value" pairs whose key looks sensitive.
    (
        re.compile(
            r"(?i)\b(" + "|".join(SENSITIVE_KEY_HINTS) + r")\b\s*[:=]\s*([^\s,;\"'}]{3,})"
        ),
        r"\1=" + _PLACEHOLDER,
    ),
    # Long high-entropy blobs (32+ chars of base64-ish data).
    (re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b"), "***redacted-blob***"),
)


def redact_text(value: Any, *, max_length: int = 4000) -> str:
    """Return a log-safe string with credentials removed and length capped."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else repr(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > max_length:
        text = text[:max_length] + f"... [truncated {len(text) - max_length} chars]"
    return text


def redact_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure for logging."""
    if depth > 6:
        return "***depth-limit***"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if is_sensitive_key(key_str):
                out[key_str] = _PLACEHOLDER
            else:
                out[key_str] = redact_value(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value, max_length=400)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(value, max_length=200)


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key name looks like it holds a secret."""
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Redact only the values of keys that look sensitive (shallow)."""
    return {
        key: (_PLACEHOLDER if is_sensitive_key(str(key)) else value)
        for key, value in mapping.items()
    }


def summarise_for_audit(values: Iterable[Any], *, limit: int = 120) -> str:
    """Compact single-line summary used by the tool audit log."""
    parts = [redact_text(item, max_length=limit) for item in values]
    joined = ", ".join(part for part in parts if part)
    return joined[:600]
