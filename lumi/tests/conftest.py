"""Shared pytest fixtures.

Every test runs against a temporary LUMI home directory, so no test can touch the
real profile, the real database or the user's real files.  Heavier fixtures (the
whole application) are opt-in per test, which keeps the suite fast.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lumi.ai.ollama import ChatChunk, ChatResponse, ModelInfo, OllamaError, OllamaStatus  # noqa: E402
from lumi.config.paths import AppPaths  # noqa: E402
from lumi.config.settings import Settings, SettingsStore  # noqa: E402
from lumi.memory.service import ConversationStore, MemoryService  # noqa: E402
from lumi.memory.store import Database  # noqa: E402
from lumi.notes.repository import NoteRepository  # noqa: E402
from lumi.reminders.focus import FocusRepository  # noqa: E402
from lumi.reminders.service import ReminderRepository, ReminderService  # noqa: E402
from lumi.security.audit import AuditLog  # noqa: E402
from lumi.security.policy import PermissionPolicy  # noqa: E402
from lumi.tasks.repository import TaskRepository  # noqa: E402
from lumi.tools.base import StopToken, ToolContext  # noqa: E402
from lumi.tools.executor import ToolExecutor  # noqa: E402
from lumi.tools.registry import ToolRegistry  # noqa: E402


@pytest.fixture()
def lumi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppPaths:
    """An isolated application home."""
    home = tmp_path / "lumi-home"
    monkeypatch.setenv("LUMI_HOME", str(home))
    paths = AppPaths(home).ensure()
    return paths


@pytest.fixture()
def settings(lumi_home: AppPaths) -> Settings:
    """Default settings, with deterministic behaviour where it matters."""
    store = SettingsStore(lumi_home)
    loaded = store.load()
    loaded.automation.workspace_folders = [str(lumi_home.home / "workspace")]
    loaded.automation.search_folders = [str(lumi_home.home)]
    return loaded


@pytest.fixture()
def settings_store(lumi_home: AppPaths, settings: Settings) -> SettingsStore:
    store = SettingsStore(lumi_home)
    store.replace(settings)
    return store


@pytest.fixture()
def db(lumi_home: AppPaths) -> Iterator[Database]:
    database = Database(lumi_home.database_file)
    database.connect()
    yield database
    database.close()


@pytest.fixture()
def tasks(db: Database) -> TaskRepository:
    return TaskRepository(db)


@pytest.fixture()
def notes(db: Database) -> NoteRepository:
    return NoteRepository(db)


@pytest.fixture()
def memory(db: Database) -> MemoryService:
    return MemoryService(db)


@pytest.fixture()
def conversations(db: Database) -> ConversationStore:
    return ConversationStore(db)


@pytest.fixture()
def reminder_repo(db: Database) -> ReminderRepository:
    return ReminderRepository(db)


@pytest.fixture()
def reminders(reminder_repo: ReminderRepository) -> ReminderService:
    return ReminderService(reminder_repo)


@pytest.fixture()
def focus(db: Database) -> FocusRepository:
    return FocusRepository(db)


@pytest.fixture()
def audit(db: Database) -> AuditLog:
    return AuditLog(db, session_id="test-session")


@pytest.fixture()
def policy(settings: Settings) -> PermissionPolicy:
    return PermissionPolicy(settings)


@pytest.fixture()
def registry() -> ToolRegistry:
    return ToolRegistry.build_default()


@pytest.fixture()
def executor(registry: ToolRegistry, policy: PermissionPolicy, audit: AuditLog) -> ToolExecutor:
    return ToolExecutor(registry, policy, audit)


@pytest.fixture()
def tool_context(
    settings: Settings,
    lumi_home: AppPaths,
    db: Database,
    audit: AuditLog,
    tasks: TaskRepository,
    notes: NoteRepository,
    memory: MemoryService,
    conversations: ConversationStore,
    reminders: ReminderService,
    reminder_repo: ReminderRepository,
    focus: FocusRepository,
    executor: ToolExecutor,
) -> ToolContext:
    """A complete tool context wired to temporary storage."""
    from lumi.automation.browser import BrowserController
    from lumi.automation.controller import AutomationController
    from lumi.automation.files import FileManager

    automation = AutomationController(settings, paths=lumi_home, audit=audit)
    engine = FakeEngine()
    return ToolContext(
        settings=settings,
        paths=lumi_home,
        db=db,
        audit=audit,
        stop_token=StopToken(),
        services={
            "tasks": tasks,
            "notes": notes,
            "memory": memory,
            "conversations": conversations,
            "reminders": reminders,
            "reminder_repo": reminder_repo,
            "focus": focus,
            "files": FileManager(PermissionPolicy(settings)),
            "automation": automation,
            "browser": BrowserController(automation, settings),
            "executor": executor,
            "engine": engine,
            "audit": audit,
        },
        session_id="test-session",
    )


@pytest.fixture()
def workspace(lumi_home: AppPaths) -> Path:
    """A writable workspace folder for file-tool tests."""
    folder = lumi_home.home / "workspace"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class FakeOllamaClient:
    """A stand-in for :class:`lumi.ai.ollama.OllamaClient` used in tests.

    It can be scripted with a queue of replies, which lets the tests cover
    streaming, tool calling, malformed output and connection failures without a
    running Ollama.
    """

    def __init__(
        self,
        *,
        running: bool = True,
        models: list[str] | None = None,
        replies: list[list[ChatChunk]] | None = None,
        capabilities: list[str] | None = None,
        version: str = "0.0.0-test",
    ) -> None:
        self.running = running
        self._models = models or []
        self.replies = list(replies or [])
        self.capabilities = capabilities if capabilities is not None else ["completion", "tools"]
        self.version_string = version
        self.calls: list[dict[str, Any]] = []
        self.pulled: list[str] = []

    # ---------------------------------------------------------------- status
    def status(self) -> OllamaStatus:
        if not self.running:
            return OllamaStatus(installed=True, running=False, base_url="http://127.0.0.1:11434", message="connection refused", error_kind="connection")
        return OllamaStatus(installed=True, running=True, version=self.version_string, base_url="http://127.0.0.1:11434", message="Connected")

    def list_models(self) -> list[ModelInfo]:
        if not self.running:
            raise OllamaError("LUMI could not reach Ollama.", kind="connection")
        return [ModelInfo(name=name, size_bytes=2 * 1024 ** 3, capabilities=list(self.capabilities)) for name in self._models]

    def model_names(self) -> list[str]:
        return [model.name for model in self.list_models()]

    def show_model(self, name: str, *, refresh: bool = False) -> ModelInfo:
        return ModelInfo(name=name, size_bytes=2 * 1024 ** 3, capabilities=list(self.capabilities))

    def supports_tools(self, name: str) -> bool:
        return "tools" in self.capabilities

    def model_details(self, name: str) -> ModelInfo:
        return self.show_model(name)

    def loaded_models(self) -> list[dict[str, Any]]:
        return []

    def unload_model(self, name: str) -> bool:
        return True

    def delete_model(self, name: str) -> bool:
        return True

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        messages,
        *,
        model: str = "",
        options=None,
        tools=None,
        stream: bool = True,
        keep_alive: str = "10m",
        stop_token=None,
        format_json: bool = False,
        read_timeout_s=None,
    ):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "model": model,
                "options": dict(options or {}),
                "tools": list(tools or []),
                "format_json": format_json,
            }
        )
        if not self.running:
            raise OllamaError("LUMI could not reach Ollama.", kind="connection")
        if not self.replies:
            yield ChatChunk(text="(no scripted reply)", done=True)
            return
        for chunk in self.replies.pop(0):
            if stop_token is not None and stop_token.cancelled:
                return
            yield chunk

    def chat_once(self, messages, *, model: str = "", options=None, tools=None, keep_alive: str = "10m", stop_token=None, format_json: bool = False):
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for chunk in self.chat(messages, model=model, options=options, tools=tools, stream=False, stop_token=stop_token, format_json=format_json):
            text_parts.append(chunk.text)
            tool_calls.extend(chunk.tool_calls)
        return ChatResponse(text="".join(text_parts), tool_calls=tool_calls, model=model)

    def pull_model(self, name: str, *, on_progress=None, stop_token=None, read_timeout: float = 60.0) -> bool:
        self.pulled.append(name)
        if on_progress is not None:
            on_progress({"status": "success"})
        return True

    def warm_up(self, model: str, *, keep_alive: str = "10m") -> bool:
        return True

    def close(self) -> None:
        return None


def scripted_reply(text: str, *, tool_calls: list[dict[str, Any]] | None = None) -> list[ChatChunk]:
    """Build a one-chunk reply for :class:`FakeOllamaClient`."""
    chunks = [ChatChunk(text=text)]
    if tool_calls:
        chunks.append(ChatChunk(text="", tool_calls=tool_calls))
    chunks.append(ChatChunk(text="", done=True, metrics={"eval_count": 12, "tokens_per_second": 20.0}))
    return chunks


class FakeEngine:
    """Stands in for :class:`lumi.ai.assistant.AssistantEngine` in tool tests.

    The system tools only need the engine for status, the installed model list
    and the Ollama client, so this covers exactly that surface - and it lets the
    tests run with no Ollama installed at all.
    """

    def __init__(self, client: FakeOllamaClient | None = None) -> None:
        self._client = client or FakeOllamaClient(running=False)

    def status(self):
        return self._client.status()

    def client(self) -> FakeOllamaClient:
        return self._client

    def list_models(self):
        if not self._client.running:
            # Behave like the real client: a dead server raises, it does not
            # pretend the machine has no models.
            raise OllamaError("LUMI could not reach Ollama.", kind="connection")
        return self._client.list_models()

    def model_names(self) -> list[str]:
        return self._client.model_names()

    def supports_tools(self, model: str) -> bool:
        return False

    def selected_model(self) -> str:
        return ""

    def invalidate_model_cache(self) -> None:
        return None

    def describe_state(self) -> dict[str, Any]:
        return {"ollama": self.status().to_dict(), "selected_model": ""}


@pytest.fixture()
def fake_client() -> FakeOllamaClient:
    return FakeOllamaClient(models=["llama3.2:3b"], replies=[scripted_reply("Hello from the fake model.")])
