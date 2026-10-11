"""A small, strict client for the local Ollama server.

Only the endpoints LUMI needs are implemented, and every call is bounded:

* **Loopback by default.**  Ollama ships listening on ``127.0.0.1``.  A remote
  host is refused unless the user explicitly allows it in settings, so the
  assistant cannot be pointed at a stranger's machine by accident.
* **No silent retries, no infinite waits.**  Every request has a timeout; a
  streaming request checks the stop token between chunks.
* **Honest errors.**  :class:`OllamaError` carries a ``kind`` so the UI can say
  "Ollama is not running" instead of printing a socket error.

The client is synchronous: it is called from a worker thread by the UI and by
the assistant engine.  ``httpx`` is used rather than ``requests`` because it is
already a hard dependency and handles streaming cleanly.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence
from urllib.parse import urlparse

from lumi.tools.base import StopToken

log = logging.getLogger("lumi.ai.ollama")

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
#: Model keep-alive after a chat: long enough to be useful, short enough to free VRAM.
DEFAULT_KEEP_ALIVE = "10m"

Message = Mapping[str, Any]


class OllamaError(Exception):
    """Any failure while talking to Ollama, with a machine-readable ``kind``."""

    KINDS = (
        "connection",
        "timeout",
        "http",
        "not_found",
        "model_missing",
        "blocked",
        "malformed",
        "memory",
        "cancelled",
    )

    def __init__(self, message: str, *, kind: str = "connection", status_code: int | None = None, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind if kind in self.KINDS else "http"
        self.status_code = status_code
        self.hint = hint or self._default_hint()

    def _default_hint(self) -> str:
        return {
            "connection": (
                "Ollama does not seem to be running. Start it from the Start menu (it lives in the "
                "system tray) and try again. LUMI keeps working without it - tasks, notes, reminders "
                "and files need no AI at all."
            ),
            "timeout": "The model took too long to answer. A smaller model, or a shorter question, usually fixes this.",
            "model_missing": "Run 'ollama pull <model>' in a terminal, or pick a different model in Settings → Models.",
            "blocked": "LUMI only talks to a local Ollama server unless you allow a remote host in Settings.",
            "memory": "There is not enough free memory for this model. Try a smaller one.",
            "malformed": "Ollama sent something LUMI did not understand. Updating Ollama usually fixes this.",
            "cancelled": "Stopped at your request.",
        }.get(self.kind, "Check the log for details, then try again.")

    def friendly(self) -> str:
        """One-line, plain-language description used in the UI."""
        return f"{self.message} {self.hint}".strip()

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.message


@dataclass
class ModelInfo:
    """One entry from ``/api/tags`` or ``/api/show``."""

    name: str
    size_bytes: int = 0
    digest: str = ""
    modified_at: str = ""
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    context_length: int = 0
    capabilities: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / (1024 ** 3), 2)

    @property
    def size_human(self) -> str:
        return human_size(self.size_bytes) if self.size_bytes else "size unknown"

    @property
    def supports_tools(self) -> bool:
        return "tools" in {item.lower() for item in self.capabilities} or "tool" in {item.lower() for item in self.capabilities}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "size_gb": self.size_gb,
            "family": self.family,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
            "context_length": self.context_length,
            "capabilities": list(self.capabilities),
            "modified_at": self.modified_at,
        }


@dataclass
class OllamaStatus:
    """Result of a health probe, designed to be shown to a human."""

    installed: bool = True
    running: bool = False
    version: str = ""
    base_url: str = DEFAULT_BASE_URL
    message: str = ""
    error_kind: str = ""
    model_count: int = 0

    @property
    def ready(self) -> bool:
        return self.running

    @property
    def label(self) -> str:
        if self.running:
            return f"Ollama {self.version}".strip()
        return "Ollama not running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "running": self.running,
            "version": self.version,
            "base_url": self.base_url,
            "message": self.message,
            "error_kind": self.error_kind,
            "model_count": self.model_count,
            "ready": self.ready,
            "label": self.label,
        }


@dataclass
class ChatChunk:
    """One streamed piece of a reply."""

    text: str = ""
    done: bool = False
    model: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def tokens_per_second(self) -> float:
        value = self.metrics.get("tokens_per_second") if self.metrics else None
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0


@dataclass
class ChatResponse:
    """A complete (non-streamed) reply."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    thinking: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    done_reason: str = ""

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


def human_size(size_bytes: float) -> str:
    """Render a byte count the way a person would say it."""
    value = float(size_bytes or 0)
    if value <= 0:
        return "unknown size"
    for unit, factor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= factor:
            amount = value / factor
            return f"{amount:.1f} {unit}" if amount < 10 else f"{amount:.0f} {unit}"
    return f"{int(value)} bytes"


def estimate_seconds_for_pull(size_bytes: float, megabits_per_second: float) -> str:
    """Rough download time, said the way a person would.

    Deliberately conservative: home connections rarely reach their advertised
    speed, so the estimate is padded by 25%.
    """
    speed = max(0.1, float(megabits_per_second or 0))
    seconds = (float(size_bytes or 0) * 8 / 1_000_000) / speed * 1.25
    if seconds <= 0:
        return "unknown"
    if seconds < 90:
        return f"about {int(round(seconds / 5.0) * 5)} seconds"
    minutes = seconds / 60
    if minutes < 90:
        return f"about {int(round(minutes))} minute(s)"
    return f"about {minutes / 60:.1f} hours"


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(base_url: str, *, allow_non_loopback: bool = False) -> str:
    """Check that the endpoint is one LUMI is allowed to talk to."""
    candidate = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise OllamaError(
            f"'{base_url}' is not an http(s) address.", kind="blocked", hint="Ollama is normally http://127.0.0.1:11434."
        )
    if parsed.username or parsed.password:
        raise OllamaError("The Ollama URL must not contain credentials.", kind="blocked")
    host = parsed.hostname or ""
    if not _is_loopback(host) and not allow_non_loopback:
        raise OllamaError(
            f"'{candidate}' is not on this computer, so LUMI will not connect to it.",
            kind="blocked",
            hint="Enable 'allow remote Ollama' in Settings → Models if this is really your own server.",
        )
    if not parsed.port:
        raise OllamaError(f"'{candidate}' has no port.", kind="blocked", hint="The default Ollama port is 11434.")
    return candidate


class OllamaClient:
    """Minimal client for the endpoints LUMI uses."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        connect_timeout_s: float = 3.0,
        read_timeout_s: float = 180.0,
        allow_non_loopback: bool = False,
        transport: Any = None,
    ) -> None:
        self.base_url = validate_base_url(base_url, allow_non_loopback=allow_non_loopback)
        self.connect_timeout_s = float(connect_timeout_s)
        self.read_timeout_s = float(read_timeout_s)
        self._transport = transport
        self._client: Any = None
        self._model_cache: dict[str, ModelInfo] = {}

    # ------------------------------------------------------------- plumbing
    def _http(self):  # pragma: no cover - thin wrapper around httpx
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.read_timeout_s, connect=self.connect_timeout_s),
                trust_env=False,  # never pick up a proxy from the environment
                headers={"User-Agent": "LUMI/1.0 (+local)"},
                transport=self._transport,  # tests inject httpx.MockTransport here
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None, timeout: float | None = None):
        client = self._http()
        try:
            response = client.request(method, path, json=payload, timeout=timeout)
        except Exception as exc:  # httpx exceptions, plus socket errors
            raise self._translate(exc) from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            if response.status_code == 404:
                # Almost always "that model is not installed" - keep the hint that
                # tells the user how to fix it rather than dumping the raw body.
                raise OllamaError(
                    f"Ollama has no model for this request ({path}).",
                    kind="model_missing",
                    status_code=404,
                )
            raise OllamaError(
                f"Ollama returned {response.status_code} for {path}.",
                kind="http",
                status_code=response.status_code,
                hint=detail or None,
            )
        return response

    def _translate(self, exc: Exception) -> OllamaError:
        name = type(exc).__name__.lower()
        text = str(exc)
        if "connect" in name or "refused" in text or "connectionerror" in name:
            return OllamaError(
                f"LUMI could not reach Ollama at {self.base_url}.", kind="connection"
            )
        if "timeout" in name or "timed out" in text:
            return OllamaError("Ollama did not answer in time.", kind="timeout")
        if "resolve" in text or "name or service" in text:
            return OllamaError(f"'{self.base_url}' could not be resolved.", kind="blocked")
        return OllamaError(f"Talking to Ollama failed ({type(exc).__name__}).", kind="http", hint=text[:200])

    # ---------------------------------------------------------------- status
    def status(self) -> OllamaStatus:
        """Probe the server.  Never raises - a missing Ollama is a normal state."""
        try:
            response = self._request("GET", "/api/version", timeout=self.connect_timeout_s)
            version = str(response.json().get("version", "")).strip()
        except OllamaError as exc:
            return OllamaStatus(
                installed=True,
                running=False,
                base_url=self.base_url,
                message=exc.message,
                error_kind=exc.kind,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return OllamaStatus(running=False, base_url=self.base_url, message=str(exc), error_kind="malformed")

        count = 0
        try:
            count = len(self.list_models())
        except OllamaError:  # pragma: no cover - version worked, tags failed
            pass
        return OllamaStatus(
            installed=True,
            running=True,
            version=version,
            base_url=self.base_url,
            message="Connected" + (f" · {count} model(s) installed" if count else " · no models installed yet"),
            model_count=count,
        )

    def is_running(self) -> bool:
        return self.status().running

    # ---------------------------------------------------------------- models
    def list_models(self) -> list[ModelInfo]:
        response = self._request("GET", "/api/tags")
        try:
            payload = response.json()
        except Exception as exc:
            raise OllamaError("Ollama sent an unreadable model list.", kind="malformed", hint=str(exc)[:200]) from exc
        models: list[ModelInfo] = []
        for entry in payload.get("models", []) or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            models.append(
                ModelInfo(
                    name=str(entry["name"]),
                    size_bytes=int(entry.get("size") or 0),
                    digest=str(entry.get("digest") or ""),
                    modified_at=str(entry.get("modified_at") or ""),
                    details=dict(entry.get("details") or {}),
                    family=str((entry.get("details") or {}).get("family") or ""),
                    parameter_size=str((entry.get("details") or {}).get("parameter_size") or ""),
                    quantization=str((entry.get("details") or {}).get("quantization_level") or ""),
                )
            )
        models.sort(key=lambda item: item.name.lower())
        for model in models:
            self._model_cache.setdefault(model.name, model)
        return models

    def model_names(self) -> list[str]:
        return [model.name for model in self.list_models()]

    def show_model(self, name: str, *, refresh: bool = False) -> ModelInfo:
        """Details for one model, including whether it supports tool calling."""
        if not refresh and name in self._model_cache and self._model_cache[name].capabilities:
            return self._model_cache[name]
        response = self._request("POST", "/api/show", payload={"model": name})
        try:
            payload = response.json()
        except Exception as exc:
            raise OllamaError(f"Could not read details for '{name}'.", kind="malformed", hint=str(exc)[:200]) from exc
        details = dict(payload.get("details") or {})
        model_info = dict(payload.get("model_info") or {})
        capabilities = [str(item) for item in (payload.get("capabilities") or [])]
        context_length = 0
        for key, value in model_info.items():
            if key.endswith(".context_length"):
                try:
                    context_length = int(value)
                except (TypeError, ValueError):
                    continue
        info = ModelInfo(
            name=name,
            family=str(details.get("family") or ""),
            parameter_size=str(details.get("parameter_size") or ""),
            quantization=str(details.get("quantization_level") or ""),
            context_length=context_length,
            capabilities=capabilities,
            details=details,
        )
        self._model_cache[name] = info
        return info

    def supports_tools(self, name: str) -> bool:
        """Conservative check: unknown models are treated as *not* tool-capable."""
        if not name:
            return False
        try:
            return self.show_model(name).supports_tools
        except OllamaError:
            return False

    def loaded_models(self) -> list[dict[str, Any]]:
        """Models currently held in memory (VRAM/RAM)."""
        try:
            response = self._request("GET", "/api/ps", timeout=self.connect_timeout_s)
            payload = response.json()
        except OllamaError:
            return []
        loaded = []
        for entry in payload.get("models", []) or []:
            if not isinstance(entry, dict):
                continue
            loaded.append(
                {
                    "name": str(entry.get("name") or entry.get("model") or ""),
                    "size_bytes": int(entry.get("size") or 0),
                    "size_vram_bytes": int(entry.get("size_vram") or 0),
                    "expires_at": str(entry.get("expires_at") or ""),
                }
            )
        return loaded

    def unload_model(self, name: str) -> bool:
        """Ask Ollama to free a model immediately (keep_alive = 0)."""
        if not name:
            return False
        try:
            self._request(
                "POST",
                "/api/generate",
                payload={"model": name, "keep_alive": 0, "prompt": ""},
                timeout=max(self.connect_timeout_s, 20.0),
            )
            return True
        except OllamaError as exc:
            log.info("could not unload %s: %s", name, exc.message)
            return False

    def delete_model(self, name: str) -> bool:
        if not name:
            return False
        try:
            self._request("DELETE", "/api/delete", payload={"model": name}, timeout=30.0)
        except OllamaError as exc:
            log.info("could not delete %s: %s", name, exc.message)
            return False
        self._model_cache.pop(name, None)
        log.info("deleted model %s", name)
        return True

    def warm_up(self, model: str, *, keep_alive: str = DEFAULT_KEEP_ALIVE) -> bool:
        """Load a model before the user needs it (best effort)."""
        if not model:
            return False
        try:
            self._request(
                "POST",
                "/api/generate",
                payload={"model": model, "prompt": "", "keep_alive": keep_alive, "options": {"num_predict": 1}},
                timeout=max(self.read_timeout_s, 60.0),
            )
            return True
        except OllamaError:
            return False

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        options: Optional[Mapping[str, Any]] = None,
        tools: Optional[Iterable[Mapping[str, Any]]] = None,
        stream: bool = True,
        keep_alive: str = DEFAULT_KEEP_ALIVE,
        stop_token: StopToken | None = None,
        format_json: bool = False,
        read_timeout_s: float | None = None,
    ) -> Iterator[ChatChunk]:
        """Stream a completion.  Yields :class:`ChatChunk`; raises OllamaError."""
        if not model:
            raise OllamaError(
                "No model is selected.", kind="model_missing", hint="Choose one in Settings → Models, or run 'ollama pull llama3.2:3b'."
            )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "stream": True,
            "keep_alive": keep_alive,
        }
        if options:
            payload["options"] = {key: value for key, value in options.items() if value is not None}
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
        if format_json:
            payload["format"] = "json"

        client = self._http()
        timeout = read_timeout_s or self.read_timeout_s
        try:
            with client.stream("POST", "/api/chat", json=payload, timeout=timeout) as response:
                if response.status_code >= 400:
                    body = self._safe_body(response)
                    kind = "model_missing" if response.status_code == 404 else "http"
                    raise OllamaError(
                        f"Ollama refused the request ({response.status_code}).",
                        kind=kind,
                        status_code=response.status_code,
                        hint=body,
                    )
                for line in response.iter_lines():
                    if stop_token is not None and stop_token.cancelled:
                        log.info("stream cancelled by the user")
                        return
                    chunk = self._parse_line(line)
                    if chunk is None:
                        continue
                    if chunk.error:
                        raise OllamaError(chunk.error, kind="http")
                    yield chunk
                    if chunk.done:
                        return
        except OllamaError:
            raise
        except Exception as exc:
            raise self._translate(exc) from exc

    def chat_once(self, messages: Sequence[Message], **kwargs: Any) -> ChatResponse:
        """Non-streaming convenience wrapper (used by the planner)."""
        kwargs.pop("stream", None)
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        model = str(kwargs.get("model") or "")
        metrics: dict[str, Any] = {}
        thinking: list[str] = []
        for chunk in self.chat(messages, stream=True, **kwargs):
            text_parts.append(chunk.text)
            tool_calls.extend(chunk.tool_calls)
            thinking.append(chunk.thinking)
            if chunk.metrics:
                metrics.update(chunk.metrics)
            if chunk.model:
                model = chunk.model
        return ChatResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            model=model,
            thinking="".join(thinking),
            metrics=metrics,
        )

    @staticmethod
    def _safe_body(response: Any) -> str:
        try:
            return str(response.text)[:300]
        except Exception:  # pragma: no cover - defensive
            return ""

    def _parse_line(self, line: Any) -> Optional[ChatChunk]:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        text = str(line or "").strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log.debug("ignoring unparsable stream line: %s", text[:120])
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("error"):
            return ChatChunk(error=str(payload["error"]))
        message = payload.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        tool_calls = [call for call in (message.get("tool_calls") or []) if isinstance(call, dict)]
        metrics: dict[str, Any] = {}
        if payload.get("done"):
            eval_count = payload.get("eval_count")
            eval_duration = payload.get("eval_duration") or 0
            if eval_count and eval_duration:
                metrics["tokens_per_second"] = round(float(eval_count) / (float(eval_duration) / 1e9), 1)
            for key in ("eval_count", "prompt_eval_count", "total_duration", "done_reason"):
                if key in payload:
                    metrics[key] = payload[key]
        return ChatChunk(
            text=str(message.get("content") or ""),
            thinking=str(message.get("thinking") or ""),
            tool_calls=tool_calls,
            done=bool(payload.get("done")),
            model=str(payload.get("model") or ""),
            metrics=metrics,
        )

    # ------------------------------------------------------------------ pull
    def pull_model(
        self,
        name: str,
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        stop_token: StopToken | None = None,
        read_timeout: float = 60.0,
    ) -> bool:
        """Download a model, reporting progress.  Cancellable, and never silent."""
        if not name or "/" in name and len(name) > 200:
            raise OllamaError(f"'{name}' does not look like a model name.", kind="model_missing")
        client = self._http()
        log.info("pulling model %s", name)
        try:
            with client.stream("POST", "/api/pull", json={"model": name, "stream": True}, timeout=read_timeout) as response:
                if response.status_code >= 400:
                    raise OllamaError(
                        f"Ollama could not pull '{name}'.",
                        kind="model_missing",
                        status_code=response.status_code,
                        hint=self._safe_body(response),
                    )
                for line in response.iter_lines():
                    if stop_token is not None and stop_token.cancelled:
                        log.info("model pull cancelled by the user")
                        return False
                    parsed = self._parse_pull_line(line)
                    if parsed is None:
                        continue
                    if parsed.get("error"):
                        raise OllamaError(str(parsed["error"]), kind="model_missing")
                    if on_progress is not None:
                        on_progress(parsed)
                    if str(parsed.get("status", "")).lower() == "success":
                        self._model_cache.pop(name, None)
                        return True
        except OllamaError:
            raise
        except Exception as exc:
            raise self._translate(exc) from exc
        return False

    @staticmethod
    def _parse_pull_line(line: Any) -> Optional[dict[str, Any]]:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        text = str(line or "").strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        total = payload.get("total")
        completed = payload.get("completed")
        if isinstance(total, int) and isinstance(completed, int) and total > 0:
            payload["percent"] = round(completed / total * 100, 1)
        return payload

    # ----------------------------------------------------------------- misc
    def disk_space_hint(self, model_size_bytes: int, free_bytes: int) -> str:
        if model_size_bytes and model_size_bytes > max(0, free_bytes - 2 * 1024 ** 3):
            return (
                "There may not be enough free disk space for that model. "
                f"It needs about {round(model_size_bytes / 1024 ** 3, 1)} GB."
            )
        return ""

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<OllamaClient {self.base_url}>"
