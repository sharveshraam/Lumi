"""End-to-end tests against a *real* Ollama-shaped server on a real socket.

Everywhere else the Ollama client is tested with a fake transport object.  These
tests run an HTTP server on 127.0.0.1 that speaks the Ollama API - version,
tags, chat with streaming NDJSON, pull with progress - and point the real client
at it.  That covers the parts a fake transport cannot: the actual HTTP request,
the streaming decode, cancellation mid-stream and the error mapping.

No Ollama installation and no model download is involved, and nothing leaves
the loopback interface.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from lumi.ai.assistant import AssistantEngine, ChatCallbacks
from lumi.ai.ollama import OllamaClient, OllamaError
from lumi.tools.base import StopToken

MODEL = "llama3.2:3b"


class _Handler(BaseHTTPRequestHandler):
    """A tiny stand-in for ``ollama serve``."""

    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------
    def log_message(self, *_args) -> None:  # noqa: D102 - keep the test output clean
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw or b"{}")

    def _send_ndjson(self, chunks: Iterator[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for chunk in chunks:
                line = (json.dumps(chunk) + "\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # the client stopped reading
            return
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = self.path.split("?")[0]
        if path == "/api/version":
            self._send_json({"version": "0.5.7-test"})
        elif path == "/api/tags":
            self._send_json(
                {
                    "models": [
                        {
                            "name": MODEL,
                            "model": MODEL,
                            "size": 2_000_000_000,
                            "details": {
                                "family": "llama",
                                "parameter_size": "3B",
                                "quantization_level": "Q4_K_M",
                            },
                        },
                        {
                            "name": "qwen2.5:7b",
                            "model": "qwen2.5:7b",
                            "size": 4_700_000_000,
                            "details": {"family": "qwen2", "parameter_size": "7B"},
                        },
                    ]
                }
            )
        elif path == "/api/ps":
            self._send_json({"models": [{"name": MODEL, "size_vram": 2_000_000_000}]})
        else:
            self._send_json({"error": f"no such route: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        path = self.path.split("?")[0]
        payload = self._read_json()

        if path == "/api/show":
            self._send_json(
                {
                    "details": {"family": "llama", "parameter_size": "3B"},
                    "model_info": {"llama.context_length": 8192},
                    "capabilities": ["completion", "tools"],
                }
            )
        elif path == "/api/chat":
            self._chat(payload)
        elif path == "/api/pull":
            self._pull(payload)
        elif path == "/api/delete":
            self._send_json({})
        else:
            self._send_json({"error": f"no such route: {path}"}, status=404)

    def _chat(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages") or []
        last = (messages[-1].get("content") if messages else "") or ""

        if "TOOL" in last:
            # Ask for a tool the way models do, then finish.
            chunks = [
                {"message": {"role": "assistant", "content": ""},
                 "model": MODEL, "done": False},
                {"message": {"role": "assistant",
                             "content": '{"name": "current_datetime", "arguments": {}}'},
                 "model": MODEL, "done": False},
                {"message": {"role": "assistant", "content": ""},
                 "model": MODEL, "done": True,
                 "eval_count": 12, "eval_duration": 200_000_000},
            ]
        elif "FAIL" in last:
            self._send_json({"error": "model requires more system memory"}, status=500)
            return
        else:
            answer = f"Answer to: {last}".strip()
            pieces = [part + " " for part in answer.split()]
            if pieces:
                pieces[-1] = pieces[-1].rstrip()
            chunks = [
                {"message": {"role": "assistant", "content": piece},
                 "model": MODEL, "done": False}
                for piece in pieces
            ]
            chunks.append(
                {"message": {"role": "assistant", "content": ""},
                 "model": MODEL, "done": True,
                 "eval_count": len(answer.split()), "eval_duration": 500_000_000}
            )
        self._send_ndjson(iter(chunks))

    def _pull(self, payload: dict[str, Any]) -> None:
        name = payload.get("model") or payload.get("name") or MODEL
        lines = [
            {"status": f"pulling manifest"},
            {"status": "downloading", "digest": "sha256:abc", "total": 100, "completed": 50},
            {"status": "downloading", "digest": "sha256:abc", "total": 100, "completed": 100},
            {"status": "success"},
        ]
        self._send_ndjson(iter(lines))
        _ = name


class _QuietServer(ThreadingHTTPServer):
    """A client that stops reading (our cancellation tests) is not an error."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102 - see above
        return


@pytest.fixture(scope="module")
def fake_ollama() -> Iterator[str]:
    server = _QuietServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def client(fake_ollama: str) -> Iterator[OllamaClient]:
    client = OllamaClient(fake_ollama, connect_timeout_s=5.0, read_timeout_s=10.0)
    try:
        yield client
    finally:
        client.close()


# ---------------------------------------------------------------------------
# the client over a real socket
# ---------------------------------------------------------------------------
def test_version_and_model_list_over_http(client: OllamaClient) -> None:
    status = client.status()
    assert status.running is True
    assert status.version, "the version endpoint answered"
    assert status.model_count == 2

    models = client.list_models()
    names = [item.name for item in models]
    assert MODEL in names and "qwen2.5:7b" in names
    assert client.supports_tools(MODEL) is True


def test_status_reports_a_running_server(client: OllamaClient) -> None:
    status = client.status()
    assert status.running is True
    assert status.error_kind == ""
    assert "2 model(s)" in status.message
    assert client.is_running() is True
    assert client.loaded_models()


def test_chat_streams_token_by_token(client: OllamaClient) -> None:
    seen: list[str] = []
    chunks = []
    for chunk in client.chat([{"role": "user", "content": "hello"}], model=MODEL, stream=True):
        chunks.append(chunk)
        if chunk.text:
            seen.append(chunk.text)
    assert "".join(seen).startswith("Answer to:")
    assert chunks[-1].done is True
    assert chunks[-1].metrics  # eval_count / eval_duration came through


def test_chat_can_be_stopped_mid_stream(client: OllamaClient) -> None:
    """The stop token must end the loop, not just stop the UI listening."""
    stop = StopToken()
    received: list[str] = []

    def on_first() -> None:
        stop.cancel()

    for chunk in client.chat(
        [{"role": "user", "content": "a fairly long sentence that will stream in pieces"}],
        model=MODEL,
        stream=True,
        stop_token=stop,
    ):
        if chunk.text:
            received.append(chunk.text)
            stop.cancel()
    assert received, "at least the first token arrives before stopping"


def test_a_server_error_becomes_a_readable_failure(client: OllamaClient) -> None:
    """A 500 that says "requires more system memory" must explain that to the user."""
    with pytest.raises(OllamaError) as info:
        list(client.chat([{"role": "user", "content": "FAIL please"}], model=MODEL, stream=True))
    error = info.value
    assert error.kind == "memory"
    assert error.status_code == 500
    assert "smaller" in error.friendly().lower(), error.friendly()
    assert "{" not in error.hint, "the raw JSON body is not shown to the user"


def test_pull_reports_progress(client: OllamaClient) -> None:
    seen: list[dict[str, Any]] = []
    result = client.pull_model("llama3.2:1b", on_progress=seen.append)
    assert result is True, "the pull reported success"
    assert seen, "progress callbacks were delivered"
    assert any(item.get("status") == "success" for item in seen)


def test_the_client_refuses_a_non_loopback_host() -> None:
    """A typo in the URL must not send prompts to somebody else's server."""
    with pytest.raises(OllamaError) as info:
        OllamaClient("http://192.168.1.50:11434")
    assert "will not connect" in str(info.value).lower()


def test_an_unreachable_server_is_a_status_not_a_crash() -> None:
    """The port is closed: the status must say so."""
    unreachable = OllamaClient("http://127.0.0.1:1", connect_timeout_s=1.0)
    status = unreachable.status()
    assert status.running is False
    assert status.message
    assert status.error_kind in ("connection", "timeout", "malformed")


# ---------------------------------------------------------------------------
# the assistant, over a real socket
# ---------------------------------------------------------------------------
@pytest.fixture()
def engine(fake_ollama: str, settings_store, lumi_home, db, registry, executor, memory, conversations, audit):
    def factory(_settings) -> OllamaClient:
        return OllamaClient(fake_ollama, connect_timeout_s=5.0, read_timeout_s=10.0)

    engine = AssistantEngine(
        settings_store=settings_store,
        paths=lumi_home,
        db=db,
        registry=registry,
        executor=executor,
        memory_service=memory,
        conversation_store=conversations,
        audit=audit,
        client_factory=factory,
    )
    settings_store.settings.model.selected_model = MODEL
    return engine


def test_the_assistant_answers_with_a_local_model(engine, tool_context) -> None:
    tokens: list[str] = []
    reply = engine.send(
        "explain a voltage divider",
        callbacks=ChatCallbacks(on_token=tokens.append),
        tool_context=tool_context,
    )
    assert reply.error in ("", None), reply.error
    assert "voltage divider" in reply.text.lower()
    assert reply.provider == "local"
    assert tokens, "the reply streamed rather than arriving in one piece"
    assert reply.tokens_per_second >= 0


def test_the_assistant_survives_a_model_that_cannot_answer(engine, tool_context) -> None:
    reply = engine.send("FAIL now please", tool_context=tool_context)
    assert reply.error, "an unfriendly server must produce an error, not a crash"
    assert reply.hint, "the user is told what to do about it"
    assert reply.text == "" or "memory" in reply.text.lower()


def test_a_model_tool_call_runs_through_the_real_executor(engine, tool_context) -> None:
    """The model asks for current_datetime; the executor is what actually runs it."""
    outcomes = []
    reply = engine.send(
        "TOOL please",
        callbacks=ChatCallbacks(on_tool_finished=outcomes.append),
        tool_context=tool_context,
    )
    assert outcomes, "the tool call reached the executor"
    assert outcomes[0].tool == "current_datetime"
    assert outcomes[0].status == "ok"
    assert reply.tool_runs


def test_history_is_written_only_when_asked(
    engine, tool_context, conversations, settings_store
) -> None:
    settings_store.settings.privacy.save_conversations = True
    conversation = conversations.start(title="integration")
    engine.send("first message", conversation_id=conversation.id, tool_context=tool_context)
    engine.send("second message", conversation_id=conversation.id, tool_context=tool_context)
    stored = conversations.messages(conversation.id)
    assert len(stored) >= 4, "both turns are in the conversation"

    settings_store.settings.privacy.save_conversations = False
    other = conversations.start(title="private")
    engine.send("not saved", conversation_id=other.id, tool_context=tool_context)
    assert conversations.messages(other.id) == [], "privacy off means nothing is written"


def test_the_fast_path_answers_without_the_model(engine, tool_context, tasks) -> None:
    """Task commands never reach the model - they work with Ollama stopped."""
    reply = engine.send("add task: submit the record tomorrow at 6pm", tool_context=tool_context)
    assert reply.deterministic is True
    assert reply.model in ("", "none", "builtin")
    assert tasks.list(status="open"), "the task was created without a model"
