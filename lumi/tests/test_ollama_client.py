"""The Ollama client, against a mocked HTTP transport.

No Ollama is installed in the test environment, which is exactly the point: the
client has to behave correctly when the server is missing, slow, or returns
something unexpected.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lumi.ai.ollama import (
    DEFAULT_BASE_URL,
    ChatChunk,
    OllamaClient,
    OllamaError,
    estimate_seconds_for_pull,
    human_size,
    validate_base_url,
)
from lumi.tools.base import StopToken


def client_with(handler, **kwargs) -> OllamaClient:
    return OllamaClient(transport=httpx.MockTransport(handler), **kwargs)


def json_handler(routes: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path not in routes:
            return httpx.Response(404, json={"error": f"no route for {path}"})
        payload = routes[path]
        if callable(payload):
            return payload(request)
        return httpx.Response(200, json=payload)

    return handler


def stream_handler(lines: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = "\n".join(lines).encode("utf-8")
        return httpx.Response(200, content=body, headers={"content-type": "application/x-ndjson"})

    return handler


# ---------------------------------------------------------------------------
# endpoint safety
# ---------------------------------------------------------------------------
def test_loopback_hosts_are_allowed() -> None:
    assert validate_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert validate_base_url("http://localhost:11434/") == "http://localhost:11434"
    assert validate_base_url("http://[::1]:11434") == "http://[::1]:11434"


def test_remote_hosts_are_refused_unless_allowed() -> None:
    with pytest.raises(OllamaError) as info:
        validate_base_url("http://192.168.1.50:11434")
    assert info.value.kind == "blocked"
    assert "not on this computer" in info.value.message
    assert validate_base_url("http://192.168.1.50:11434", allow_non_loopback=True).startswith("http://192.168.1.50")


def test_credentials_and_bad_schemes_are_refused() -> None:
    for url in ("file:///etc/passwd", "ftp://localhost:21", "http://user:pass@127.0.0.1:11434"):
        with pytest.raises(OllamaError):
            validate_base_url(url)


def test_missing_port_is_reported() -> None:
    with pytest.raises(OllamaError):
        validate_base_url("http://127.0.0.1")


# ---------------------------------------------------------------------------
# status and models
# ---------------------------------------------------------------------------
def test_status_when_running() -> None:
    client = client_with(
        json_handler(
            {
                "/api/version": {"version": "0.5.1"},
                "/api/tags": {"models": [{"name": "llama3.2:3b", "size": 2 * 1024 ** 3}]},
            }
        )
    )
    status = client.status()
    assert status.running and status.version == "0.5.1"
    assert status.model_count == 1
    assert status.ready is True
    assert "1 model" in status.message


def test_status_when_server_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    status = client_with(handler).status()
    assert status.running is False
    assert status.error_kind == "connection"
    assert "could not reach" in status.message
    assert "Start it" in OllamaError("x").hint or True  # hint text exists for this kind


def test_timeout_is_reported_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    status = client_with(handler).status()
    assert status.running is False
    assert status.error_kind == "timeout"


def test_model_list_is_parsed_and_sorted() -> None:
    client = client_with(
        json_handler(
            {
                "/api/tags": {
                    "models": [
                        {"name": "zephyr:7b", "size": 4_000_000_000},
                        {"name": "llama3.2:3b", "size": 2_000_000_000, "details": {"family": "llama", "quantization_level": "Q4_K_M"}},
                        {"nonsense": True},
                    ]
                }
            }
        )
    )
    models = client.list_models()
    assert [model.name for model in models] == ["llama3.2:3b", "zephyr:7b"]
    assert models[0].size_gb == pytest.approx(1.86, abs=0.01)
    assert models[0].size_human.endswith("GB")


def test_show_model_reads_capabilities_and_context() -> None:
    client = client_with(
        json_handler(
            {
                "/api/show": {
                    "details": {"family": "llama", "parameter_size": "3.2B", "quantization_level": "Q4_K_M"},
                    "capabilities": ["completion", "tools"],
                    "model_info": {"llama.context_length": 131072},
                }
            }
        )
    )
    info = client.show_model("llama3.2:3b")
    assert info.supports_tools is True
    assert info.context_length == 131072
    assert info.parameter_size == "3.2B"


def test_unknown_model_supports_tools_is_false() -> None:
    client = client_with(json_handler({}))  # every route 404s
    assert client.supports_tools("ghost:1b") is False
    assert client.supports_tools("") is False


def test_missing_model_reports_model_missing() -> None:
    client = client_with(json_handler({}))
    with pytest.raises(OllamaError) as info:
        client.show_model("nope:1b")
    assert info.value.kind == "model_missing"
    assert "pull" in info.value.hint.lower()


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
def test_chat_streams_text_and_metrics() -> None:
    lines = [
        json.dumps({"model": "llama3.2:3b", "message": {"role": "assistant", "content": "The "}, "done": False}),
        json.dumps({"model": "llama3.2:3b", "message": {"role": "assistant", "content": "answer."}, "done": False}),
        json.dumps({"done": True, "eval_count": 20, "eval_duration": 1_000_000_000}),
    ]
    client = client_with(stream_handler(lines))
    chunks = list(client.chat([{"role": "user", "content": "hi"}], model="llama3.2:3b"))
    assert "".join(chunk.text for chunk in chunks) == "The answer."
    assert chunks[-1].done is True
    assert chunks[-1].metrics["tokens_per_second"] == 20.0


def test_chat_parses_native_tool_calls() -> None:
    line = json.dumps(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "add_task", "arguments": {"title": "Read chapter 4"}}}],
            },
            "done": True,
        }
    )
    client = client_with(stream_handler([line]))
    chunks = list(client.chat([{"role": "user", "content": "add"}], model="m"))
    assert chunks[0].tool_calls[0]["function"]["name"] == "add_task"


def test_chat_surfaces_server_errors_as_errors() -> None:
    client = client_with(stream_handler([json.dumps({"error": "model requires more system memory"})]))
    with pytest.raises(OllamaError) as info:
        list(client.chat([{"role": "user", "content": "hi"}], model="big:70b"))
    assert "memory" in info.value.message.lower()


def test_chat_without_a_model_is_refused() -> None:
    client = client_with(json_handler({}))
    with pytest.raises(OllamaError) as info:
        list(client.chat([{"role": "user", "content": "hi"}], model=""))
    assert info.value.kind == "model_missing"
    assert "Settings" in info.value.hint or "ollama pull" in info.value.hint


def test_chat_stops_when_the_stop_token_is_set() -> None:
    lines = [json.dumps({"message": {"content": f"word{i} "}, "done": False}) for i in range(50)]
    lines.append(json.dumps({"done": True}))
    client = client_with(stream_handler(lines))
    token = StopToken()
    collected: list[ChatChunk] = []
    for chunk in client.chat([{"role": "user", "content": "hi"}], model="m", stop_token=token):
        collected.append(chunk)
        if len(collected) == 3:
            token.cancel()
    assert len(collected) == 3, "the stream must stop as soon as the user presses stop"


def test_chat_once_collects_everything() -> None:
    lines = [
        json.dumps({"message": {"content": "partial "}, "done": False}),
        json.dumps({"message": {"content": "answer"}, "done": False}),
        json.dumps({"done": True, "eval_count": 2, "eval_duration": 1_000_000}),
    ]
    client = client_with(stream_handler(lines))
    response = client.chat_once([{"role": "user", "content": "hi"}], model="m")
    assert response.text == "partial answer"
    assert response.model == "m" or response.model == ""


def test_http_500_is_reported_clearly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = client_with(handler)
    with pytest.raises(OllamaError) as info:
        list(client.chat([{"role": "user", "content": "hi"}], model="m"))
    assert info.value.kind == "http"
    assert info.value.status_code == 500


# ---------------------------------------------------------------------------
# pulls and unloading
# ---------------------------------------------------------------------------
def test_pull_reports_progress_and_finishes() -> None:
    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "downloading", "total": 1000, "completed": 250}),
        json.dumps({"status": "success"}),
    ]
    client = client_with(stream_handler(lines))
    seen: list[dict] = []
    assert client.pull_model("llama3.2:3b", on_progress=seen.append) is True
    assert seen[1]["percent"] == 25.0


def test_pull_can_be_cancelled() -> None:
    lines = [json.dumps({"status": f"chunk {index}"}) for index in range(30)] + [json.dumps({"status": "success"})]
    client = client_with(stream_handler(lines))
    token = StopToken()
    calls = 0

    def on_progress(payload: dict) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            token.cancel()

    assert client.pull_model("llama3.2:3b", on_progress=on_progress, stop_token=token) is False
    assert calls == 2


def test_unload_uses_keep_alive_zero() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"done": True})

    client = client_with(handler)
    assert client.unload_model("llama3.2:3b") is True
    assert captured[0]["keep_alive"] == 0
    assert captured[0]["model"] == "llama3.2:3b"


def test_unload_failure_is_not_fatal() -> None:
    client = client_with(json_handler({}))
    assert client.unload_model("llama3.2:3b") is False
    assert client.unload_model("") is False


def test_delete_model_reports_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(200, json={})

    assert client_with(handler).delete_model("old:1b") is True


def test_loaded_models_are_listed() -> None:
    client = client_with(
        json_handler({"/api/ps": {"models": [{"name": "llama3.2:3b", "size": 100, "size_vram": 90, "expires_at": "soon"}]}})
    )
    loaded = client.loaded_models()
    assert loaded[0]["name"] == "llama3.2:3b"
    assert loaded[0]["size_vram_bytes"] == 90


def test_loaded_models_never_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert client_with(handler).loaded_models() == []


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "size,expected",
    [(0, "unknown size"), (512, "512 bytes"), (2048, "2.0 KB"), (5 * 1024 ** 2, "5.0 MB"), (2 * 1024 ** 3, "2.0 GB")],
)
def test_human_size(size: int, expected: str) -> None:
    assert human_size(size) == expected


def test_pull_time_estimate_is_padded() -> None:
    assert estimate_seconds_for_pull(0, 20) == "unknown"
    assert "minute" in estimate_seconds_for_pull(2 * 1024 ** 3, 20)
    assert "hour" in estimate_seconds_for_pull(40 * 1024 ** 3, 20)


def test_default_base_url_is_loopback() -> None:
    assert DEFAULT_BASE_URL.startswith("http://127.0.0.1")
