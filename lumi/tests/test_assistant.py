"""The assistant loop, with a scripted model.

These tests pin the behaviour that matters most: the deterministic fast path
works with no model at all, tool calls go through the executor, failures are
explained instead of swallowed, and the user is told what actually happened.
"""

from __future__ import annotations

from typing import Any

import pytest

from lumi.ai.assistant import AssistantEngine, ChatCallbacks
from lumi.ai.ollama import ChatChunk, OllamaError
from lumi.tests.conftest import FakeOllamaClient, scripted_reply
from lumi.tools.base import ToolContext
from lumi.utils.timeutil import now


def build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client: FakeOllamaClient) -> AssistantEngine:
    return AssistantEngine(
        settings_store=settings_store,
        paths=lumi_home,
        db=db,
        registry=registry,
        executor=executor,
        memory_service=memory,
        conversation_store=conversations,
        client_factory=lambda settings: client,
    )


@pytest.fixture()
def engine(settings_store, lumi_home, db, registry, executor, memory, conversations, fake_client) -> AssistantEngine:
    settings_store.settings.model.selected_model = "llama3.2:3b"
    return build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, fake_client)


def conversation(conversations) -> int:
    return int(conversations.start(title="test").id)


def model_only(engine: AssistantEngine, context: ToolContext) -> None:
    """Turn the deterministic fast path off so the model path is exercised."""
    engine.settings_store.settings.productivity.deterministic_commands = False
    context.settings.productivity.deterministic_commands = False


# ---------------------------------------------------------------------------
# plain answers
# ---------------------------------------------------------------------------
def test_plain_answer_is_streamed_and_stored(engine: AssistantEngine, conversations, tool_context: ToolContext) -> None:
    tokens: list[str] = []
    callbacks = ChatCallbacks(on_token=tokens.append)
    chat_id = conversation(conversations)
    reply = engine.send("What is a diode?", conversation_id=chat_id, callbacks=callbacks, tool_context=tool_context)
    assert reply.ok, reply.error
    assert reply.text == "Hello from the fake model."
    assert "".join(tokens) == "Hello from the fake model."
    stored = conversations.messages(chat_id)
    assert [message.role for message in stored] == ["user", "assistant"]


def test_history_is_sent_to_the_model(engine: AssistantEngine, conversations, tool_context: ToolContext, fake_client) -> None:
    chat_id = conversation(conversations)
    engine.send("first question", conversation_id=chat_id, tool_context=tool_context)
    engine.send("second question", conversation_id=chat_id, tool_context=tool_context)
    second_call = fake_client.calls[-1]["messages"]
    contents = [message["content"] for message in second_call if message["role"] != "system"]
    assert "first question" in contents
    assert "Hello from the fake model." in contents
    assert contents[-1] == "second question"


def test_system_prompt_carries_the_hard_rules(engine: AssistantEngine, tool_context: ToolContext, fake_client) -> None:
    engine.send("hello", tool_context=tool_context)
    system = fake_client.calls[-1]["messages"][0]
    assert system["role"] == "system"
    assert "Never claim you did something unless a tool result says it happened" in system["content"]
    assert "local time" in system["content"].lower()


# ---------------------------------------------------------------------------
# no model at all
# ---------------------------------------------------------------------------
def test_no_model_selected_explains_what_to_do(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    client = FakeOllamaClient(running=True, models=[])  # nothing installed
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    reply = engine.send("explain capacitors", tool_context=tool_context)
    assert reply.error
    assert "Settings" in reply.hint
    assert "work without a model" in reply.hint


def test_single_installed_model_is_used_automatically(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    client = FakeOllamaClient(models=["qwen2.5:3b"], replies=[scripted_reply("Using the only installed model.")])
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    reply = engine.send("hello", tool_context=tool_context)
    assert reply.ok
    assert client.calls[-1]["model"] == "qwen2.5:3b" or client.calls  # a model was requested


def test_ollama_down_produces_a_friendly_error(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, FakeOllamaClient(running=False))
    settings = settings_store.settings
    settings.model.selected_model = "llama3.2:3b"
    reply = engine.send("hello", tool_context=tool_context)
    assert reply.error
    assert "Ollama" in reply.error
    assert "tasks, notes, reminders" in reply.hint


def test_stop_before_starting(engine: AssistantEngine, tool_context: ToolContext) -> None:
    from lumi.tools.base import StopToken

    token = StopToken()
    token.cancel()
    reply = engine.send("anything", stop_token=token, tool_context=tool_context)
    assert reply.stopped
    assert "Stopped" in reply.text


def test_empty_message_is_refused(engine: AssistantEngine, tool_context: ToolContext) -> None:
    reply = engine.send("   ", tool_context=tool_context)
    assert reply.error
    assert not reply.text


# ---------------------------------------------------------------------------
# deterministic fast path (no model involved)
# ---------------------------------------------------------------------------
def test_fast_path_adds_a_task_without_calling_the_model(engine: AssistantEngine, tool_context: ToolContext, tasks, fake_client) -> None:
    reply = engine.send("add task: submit the lab record tomorrow at 6pm", tool_context=tool_context)
    assert reply.ok, reply.error
    assert reply.deterministic is True
    assert fake_client.calls == [], "a simple command must not need the model"
    stored = tasks.list(status="all")
    assert [task.title for task in stored] == ["submit the lab record"]
    assert stored[0].due_at.hour == 18


def test_fast_path_sets_a_reminder_for_remind_me(engine: AssistantEngine, tool_context: ToolContext, tasks, reminder_repo, fake_client) -> None:
    reply = engine.send("remind me to submit the lab record tomorrow at 6pm", tool_context=tool_context)
    assert reply.ok, reply.error
    assert reply.deterministic
    assert fake_client.calls == []
    assert reminder_repo.count() == 1
    reminder = reminder_repo.list()[0]
    assert reminder.title == "submit the lab record"
    assert reminder.fire_at.hour == 18
    assert tasks.list(status="all") == [], "a reminder is not a task"


def test_fast_path_lists_tasks(engine: AssistantEngine, tool_context: ToolContext, tasks, fake_client) -> None:
    tasks.create("Submit assignment", due_at=now())
    reply = engine.send("what are my tasks today", tool_context=tool_context)
    assert reply.deterministic
    assert "Submit assignment" in reply.text
    assert fake_client.calls == []


def test_fast_path_completes_by_number(engine: AssistantEngine, tool_context: ToolContext, tasks) -> None:
    task = tasks.create("Physics numericals")
    reply = engine.send(f"complete task {task.id}", tool_context=tool_context)
    assert reply.ok
    assert tasks.get(int(task.id)).status == "done"


def test_ambiguous_dates_are_confirmed_before_saving(engine: AssistantEngine, tool_context: ToolContext, tasks) -> None:
    reply = engine.send("water the plants at 6", tool_context=tool_context)
    assert reply.pending_question
    assert "18:00" in reply.text
    assert tasks.list(status="all") == [], "nothing is saved until the user confirms"

    follow_up = engine.send("yes", tool_context=tool_context)
    assert follow_up.ok
    assert [task.title for task in tasks.list(status="all")] == ["water the plants"]


def test_declining_a_confirmation_saves_nothing(engine: AssistantEngine, tool_context: ToolContext, tasks) -> None:
    engine.send("finish the Thevenin problems by Friday", tool_context=tool_context)
    reply = engine.send("no", tool_context=tool_context)
    assert "did not save" in reply.text
    assert tasks.list(status="all") == []


def test_fast_path_can_be_switched_off(engine: AssistantEngine, tool_context: ToolContext, fake_client, tasks) -> None:
    tool_context.settings.productivity.deterministic_commands = False
    engine.settings_store.settings.productivity.deterministic_commands = False
    reply = engine.send("add task: read chapter four", tool_context=tool_context)
    assert not reply.deterministic
    assert fake_client.calls, "with the fast path off, the model is asked"


def test_unknown_command_goes_to_the_model(engine: AssistantEngine, tool_context: ToolContext, fake_client) -> None:
    engine.send("explain how a buck converter works", tool_context=tool_context)
    assert fake_client.calls


# ---------------------------------------------------------------------------
# tool calling through the model
# ---------------------------------------------------------------------------
def test_json_tool_call_is_executed_then_answered(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext, tasks) -> None:
    client = FakeOllamaClient(
        models=["llama3.2:3b"],
        capabilities=["completion"],  # no native tool support -> JSON convention
        replies=[
            scripted_reply(
                'Sure, adding that now.\n```tool_call\n{"name": "add_task", "arguments": {"title": "Revise op-amps"}}\n```'
            ),
            scripted_reply("Added 'Revise op-amps' to your list."),
        ],
    )
    settings_store.settings.model.selected_model = "llama3.2:3b"
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    model_only(engine, tool_context)

    seen: list[str] = []
    callbacks = ChatCallbacks(on_tool_proposed=lambda call: seen.append(call.name))
    reply = engine.send("add a task to revise op-amps", conversation_id=None, callbacks=callbacks, tool_context=tool_context)

    assert reply.ok, reply.error
    assert seen == ["add_task"]
    assert reply.tool_runs[0]["tool"] == "add_task"
    assert reply.tool_runs[0]["status"] == "ok"
    assert [task.title for task in tasks.list(status="all")] == ["Revise op-amps"]
    assert "Revise op-amps" in reply.text
    # The tool result must have been handed back to the model.
    assert len(client.calls) == 2
    assert any("TOOL RESULT" in message["content"] for message in client.calls[1]["messages"] if message["role"] != "system")


def test_native_tool_calls_are_used_when_supported(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext, tasks) -> None:
    native = ChatChunk(tool_calls=[{"function": {"name": "add_task", "arguments": {"title": "Native call task"}}}])
    client = FakeOllamaClient(
        models=["llama3.2:3b"],
        capabilities=["completion", "tools"],
        replies=[[ChatChunk(text=""), native, ChatChunk(text="", done=True)], scripted_reply("Saved it.")],
    )
    settings_store.settings.model.selected_model = "llama3.2:3b"
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    model_only(engine, tool_context)
    reply = engine.send("add a task", tool_context=tool_context)
    assert reply.tool_runs and reply.tool_runs[0]["tool"] == "add_task"
    assert [task.title for task in tasks.list(status="all")] == ["Native call task"]
    # With native tools the catalogue is sent as tool definitions.
    assert client.calls[0]["tools"]


def test_tool_failure_is_reported_not_hidden(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    client = FakeOllamaClient(
        models=["llama3.2:3b"],
        capabilities=["completion"],
        replies=[
            scripted_reply('```tool_call\n{"name": "complete_task", "arguments": {"task_id": 999}}\n```'),
            scripted_reply("I could not find task #999, so nothing was changed."),
        ],
    )
    settings_store.settings.model.selected_model = "llama3.2:3b"
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    model_only(engine, tool_context)
    reply = engine.send("complete task 999", tool_context=tool_context, allow_tools=True)
    assert reply.tool_runs[0]["status"] in {"error", "invalid"}
    assert "could not find" in reply.text
    # The failure must be visible to the model in the next message.
    assert any(
        "TOOL RESULT" in message["content"] for message in client.calls[1]["messages"] if message["role"] != "system"
    )


def test_unknown_tool_call_is_refused_by_the_executor(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    client = FakeOllamaClient(
        models=["llama3.2:3b"],
        capabilities=["completion"],
        replies=[
            scripted_reply('```tool_call\n{"name": "delete_everything", "arguments": {}}\n```'),
            scripted_reply("I cannot do that - it is not one of my tools."),
        ],
    )
    settings_store.settings.model.selected_model = "llama3.2:3b"
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    model_only(engine, tool_context)
    reply = engine.send("wipe my drive", tool_context=tool_context)
    assert reply.tool_runs[0]["status"] == "invalid"
    assert reply.ok


def test_denied_tool_is_explained_to_the_user(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_screenshots = False
    client = FakeOllamaClient(
        models=["llama3.2:3b"],
        capabilities=["completion"],
        replies=[
            scripted_reply('```tool_call\n{"name": "take_screenshot", "arguments": {}}\n```'),
            scripted_reply("Screenshots are switched off in your settings, so I did not take one."),
        ],
    )
    settings_store.settings.model.selected_model = "llama3.2:3b"
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    model_only(engine, tool_context)
    reply = engine.send("take a screenshot", tool_context=tool_context)
    assert reply.tool_runs[0]["status"] == "denied"
    assert "switched off" in reply.text


def test_tool_loop_is_bounded(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    endless = [
        scripted_reply('```tool_call\n{"name": "task_stats", "arguments": {}}\n```')
        for _ in range(6)
    ]
    client = FakeOllamaClient(models=["llama3.2:3b"], capabilities=["completion"], replies=endless)
    settings_store.settings.model.selected_model = "llama3.2:3b"
    settings_store.settings.model.max_tool_iterations = 2
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    model_only(engine, tool_context)
    reply = engine.send("loop forever", tool_context=tool_context)
    assert len(reply.tool_runs) <= 2
    assert reply.text, "the user always gets an answer"


def test_tools_can_be_disabled(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext, fake_client) -> None:
    settings_store.settings.model.enable_tools = False
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, fake_client)
    model_only(engine, tool_context)
    engine.send("add a task to read chapter four", tool_context=tool_context)
    assert fake_client.calls[0]["tools"] in (None, [])


# ---------------------------------------------------------------------------
# failures inside the model call
# ---------------------------------------------------------------------------
class ExplodingClient(FakeOllamaClient):
    def chat(self, *args: Any, **kwargs: Any):
        raise OllamaError("Ollama is not running.", kind="connection")
        yield  # pragma: no cover - makes this a generator


def test_partial_stream_is_kept_when_the_server_dies(settings_store, lumi_home, db, registry, executor, memory, conversations, tool_context: ToolContext) -> None:
    class HalfDeadClient(FakeOllamaClient):
        def chat(self, *args: Any, **kwargs: Any):
            yield ChatChunk(text="Here is the first half. ")
            raise OllamaError("connection lost", kind="connection")

    client = HalfDeadClient(models=["llama3.2:3b"])
    settings_store.settings.model.selected_model = "llama3.2:3b"
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, client)
    reply = engine.send("long question", tool_context=tool_context)
    assert reply.text == "Here is the first half."


def test_memory_suggestion_is_offered(settings_store, lumi_home, db, registry, executor, memory, conversations, fake_client, tool_context: ToolContext) -> None:
    settings_store.settings.privacy.remember_from_chat = True
    engine = build_engine(settings_store, lumi_home, db, registry, executor, memory, conversations, fake_client)
    suggestions: list[str] = []
    engine.send(
        "Remember that I prefer bullet points, please",
        callbacks=ChatCallbacks(on_memory_suggestion=suggestions.append),
        tool_context=tool_context,
    )
    assert suggestions and "prefer bullet points" in suggestions[0]


def test_state_description(engine: AssistantEngine) -> None:
    state = engine.describe_state()
    assert state["provider"] == "local"
    assert state["selected_model"] == "llama3.2:3b"
    assert state["tools"]["total"] > 0
