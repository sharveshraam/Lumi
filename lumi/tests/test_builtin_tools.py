"""Built-in tools, exercised through the real executor.

Anything that fails here would also fail in front of the user, so each test
asserts on behaviour (what ends up in the database or on disk) rather than on
the exact wording of a summary.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from lumi.tools.base import ToolContext
from lumi.tools.executor import ToolCall, ToolExecutor
from lumi.utils.timeutil import now


def call(executor: ToolExecutor, context: ToolContext, _tool: str, **arguments):
    """Run a tool the way the assistant does, but approve any confirmation."""
    return executor.execute(ToolCall(_tool, arguments), context, confirmation_provider=lambda request: True)


def declined(executor: ToolExecutor, context: ToolContext, _tool: str, **arguments):
    """Run a tool with no confirmation channel at all."""
    original = context.confirm
    context.confirm = None
    try:
        return executor.execute(ToolCall(_tool, arguments), context, allow_confirmation=False)
    finally:
        context.confirm = original


def allow_writes(context: ToolContext) -> None:
    """Grant the file-write permission for tests that need it (it is off by default)."""
    context.settings.automation.allow_file_writes = True


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------
def test_current_datetime(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "current_datetime")
    assert outcome.ok, outcome.message
    assert outcome.result.data["date"] == now().strftime("%Y-%m-%d")
    assert outcome.result.data["weekday"]


def test_hardware_info_is_honest(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "hardware_info")
    assert outcome.ok
    data = outcome.result.data
    # Never invent a GPU: this sandbox has none, so the list must be empty.
    assert data["gpus"] == []
    assert data["ram_total_gb"] > 0
    assert "none detected" in outcome.result.summary
    assert "RAM budget" in outcome.result.summary


def test_capabilities_explains_permissions(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "capabilities", include_tools=True)
    assert outcome.ok
    text = outcome.result.summary
    assert "Needs your permission" in text
    assert "never" in text.lower()  # shells / cookies are refused, and it says so
    available = {entry["name"] for entry in outcome.result.data["tools"]}
    assert {"add_task", "create_note", "set_reminder"} <= available


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
def test_add_task_and_list(executor: ToolExecutor, tool_context: ToolContext, tasks) -> None:
    outcome = call(executor, tool_context, "add_task", title="Submit lab record", due="tomorrow 6pm", priority="high")
    assert outcome.ok, outcome.message
    task_id = outcome.result.data["task"]["id"]
    stored = tasks.get(task_id)
    assert stored.title == "Submit lab record"
    assert stored.priority == "high"
    assert stored.due_at.hour == 18

    listing = call(executor, tool_context, "list_tasks", scope="open")
    assert listing.ok
    assert "Submit lab record" in listing.result.summary
    assert listing.result.data["stats"]["open"] == 1


def test_add_task_from_text_creates_a_reminder_too(executor: ToolExecutor, tool_context: ToolContext, tasks, reminder_repo) -> None:
    outcome = call(executor, tool_context, "add_task_from_text", text="remind me to revise op-amps tomorrow at 7pm")
    assert outcome.ok, outcome.message
    task = tasks.list(status="all")[0]
    assert task.title == "revise op-amps"
    assert task.due_at.hour == 19
    assert reminder_repo.count() == 1, "a 'remind me' task must also schedule a reminder"


def test_add_task_from_text_reports_ambiguity(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "add_task_from_text", text="water the plants at 6")
    assert outcome.ok
    assert outcome.result.data["assumptions"], "an assumption must be reported back"
    assert "18:00" in outcome.result.summary


def test_complete_task_by_id_and_by_title(executor: ToolExecutor, tool_context: ToolContext, tasks) -> None:
    first = call(executor, tool_context, "add_task", title="Read chapter 4").result.data["task"]["id"]
    outcome = call(executor, tool_context, "complete_task", task_id=first)
    assert outcome.ok
    assert tasks.get(first).status == "done"

    call(executor, tool_context, "add_task", title="Electronics assignment")
    by_title = call(executor, tool_context, "complete_task", task="Electronics assignment")
    assert by_title.ok, by_title.message


def test_ambiguous_title_is_refused_with_suggestions(executor: ToolExecutor, tool_context: ToolContext, tasks) -> None:
    call(executor, tool_context, "add_task", title="Electronics assignment")
    call(executor, tool_context, "add_task", title="Electromagnetics homework")
    outcome = call(executor, tool_context, "complete_task", task="electro")
    assert not outcome.ok
    assert "matches more than one" in outcome.result.error
    assert "#1" in outcome.result.error and "#2" in outcome.result.error
    assert all(task.status == "pending" for task in tasks.list(status="open"))


def test_update_postpone_and_delete(executor: ToolExecutor, tool_context: ToolContext, tasks) -> None:
    task_id = call(executor, tool_context, "add_task", title="Physics numericals", due="today 5pm").result.data["task"]["id"]
    updated = call(executor, tool_context, "update_task", task_id=task_id, priority="urgent", category="college")
    assert updated.ok
    assert tasks.get(task_id).priority == "urgent"
    assert tasks.get(task_id).category == "college"

    postponed = call(executor, tool_context, "postpone_task", task_id=task_id, days=2)
    assert postponed.ok
    assert tasks.get(task_id).due_at > now() + timedelta(days=1)

    deleted = call(executor, tool_context, "delete_task", task_id=task_id)
    assert deleted.ok
    assert tasks.get(task_id) is None


def test_delete_task_needs_confirmation(executor: ToolExecutor, tool_context: ToolContext, tasks) -> None:
    task_id = call(executor, tool_context, "add_task", title="Temporary").result.data["task"]["id"]
    outcome = declined(executor, tool_context, "delete_task", task_id=task_id)
    assert outcome.status == "declined"
    assert tasks.get(task_id) is not None, "a declined delete must change nothing"


def test_task_stats_and_order(executor: ToolExecutor, tool_context: ToolContext) -> None:
    call(executor, tool_context, "add_task", title="Overdue", due="yesterday")
    call(executor, tool_context, "add_task", title="Soon", due="today 23:00")
    stats = call(executor, tool_context, "task_stats")
    assert stats.ok
    assert stats.result.data["stats"]["open"] == 2
    assert stats.result.data["stats"]["overdue"] == 1

    order = call(executor, tool_context, "suggest_task_order", limit=5)
    assert order.ok
    assert order.result.data["tasks"][0]["title"] == "Overdue"


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------
def test_create_and_read_note(executor: ToolExecutor, tool_context: ToolContext, notes) -> None:
    created = call(executor, tool_context, "create_note", title="Thevenin steps", body="1. Remove the load\n2. Find Rth")
    assert created.ok, created.message
    note_id = created.result.data["note"]["id"]
    assert notes.get(note_id).title == "Thevenin steps"

    read = call(executor, tool_context, "read_note", note_id=note_id)
    assert read.ok
    assert "Remove the load" in read.result.summary


def test_note_search_update_and_pin(executor: ToolExecutor, tool_context: ToolContext, notes) -> None:
    note_id = call(
        executor, tool_context, "create_note", title="Op-amp notes", body="virtual short between inputs"
    ).result.data["note"]["id"]
    found = call(executor, tool_context, "search_notes", query="virtual short")
    assert found.ok
    assert found.result.data["notes"][0]["id"] == note_id

    call(executor, tool_context, "update_note", note_id=note_id, body="virtual short; ideal input impedance")
    assert "impedance" in notes.get(note_id).body
    call(executor, tool_context, "pin_note", note_id=note_id, pinned=True)
    assert notes.get(note_id).pinned is True


def test_update_note_can_append(executor: ToolExecutor, tool_context: ToolContext, notes) -> None:
    note_id = call(executor, tool_context, "create_note", title="Log", body="first line").result.data["note"]["id"]
    call(executor, tool_context, "update_note", note_id=note_id, append="second line")
    body = notes.get(note_id).body
    assert "first line" in body and "second line" in body


def test_note_delete_needs_confirmation(executor: ToolExecutor, tool_context: ToolContext, notes) -> None:
    note_id = call(executor, tool_context, "create_note", title="Scratch", body="temporary").result.data["note"]["id"]
    denied = declined(executor, tool_context, "delete_note", note_id=note_id)
    assert denied.status == "declined"
    assert notes.get(note_id) is not None

    confirmed = call(executor, tool_context, "delete_note", note_id=note_id)
    assert confirmed.ok
    assert notes.get(note_id) is None


def test_note_titles_are_derived_when_missing(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "create_note", body="Remember to buy a new multimeter probe")
    assert outcome.ok
    assert outcome.result.data["note"]["title"].startswith("Remember to buy")


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------
def test_set_and_list_reminder(executor: ToolExecutor, tool_context: ToolContext, reminder_repo) -> None:
    outcome = call(executor, tool_context, "set_reminder", title="Call the lab assistant", when="in 2 hours")
    assert outcome.ok, outcome.message
    reminder = reminder_repo.get(outcome.result.data["reminder"]["id"])
    assert reminder.fire_at > now() + timedelta(minutes=100)

    listing = call(executor, tool_context, "list_reminders")
    assert listing.ok
    assert "Call the lab assistant" in listing.result.summary


def test_set_reminder_without_a_time_is_explained(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "set_reminder", title="Stretch")
    assert outcome.ok
    assert "15 minutes" in outcome.result.summary


def test_snooze_and_cancel_reminder(executor: ToolExecutor, tool_context: ToolContext, reminder_repo) -> None:
    reminder_id = call(executor, tool_context, "set_reminder", title="Take a break", when="in 10 minutes").result.data[
        "reminder"
    ]["id"]
    before = reminder_repo.get(reminder_id).fire_at
    snoozed = call(executor, tool_context, "snooze_reminder", reminder_id=reminder_id, minutes=20)
    assert snoozed.ok
    assert reminder_repo.get(reminder_id).fire_at > before

    cancelled = call(executor, tool_context, "cancel_reminder", reminder_id=reminder_id)
    assert cancelled.ok
    assert reminder_repo.get(reminder_id) is None or reminder_repo.get(reminder_id).enabled is False


def test_reminder_from_natural_text(executor: ToolExecutor, tool_context: ToolContext, reminder_repo) -> None:
    outcome = call(executor, tool_context, "set_reminder", text="remind me to submit the form tomorrow at 9am")
    assert outcome.ok, outcome.message
    reminder = reminder_repo.get(outcome.result.data["reminder"]["id"])
    assert reminder.fire_at.hour == 9
    assert reminder.fire_at.date() == (now() + timedelta(days=1)).date()


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------
def test_remember_and_recall(executor: ToolExecutor, tool_context: ToolContext, memory) -> None:
    outcome = call(executor, tool_context, "remember", content="Prefers diagrams over long paragraphs", kind="preference")
    assert outcome.ok, outcome.message
    assert memory.total() == 1

    recalled = call(executor, tool_context, "recall", query="diagrams")
    assert recalled.ok
    assert "diagrams" in recalled.result.summary


def test_remember_refuses_secrets(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "remember", content="my password is hunter2")
    assert not outcome.ok
    assert "credential" in outcome.result.error.lower() or "password" in outcome.result.error.lower()


def test_forget_memory(executor: ToolExecutor, tool_context: ToolContext, memory) -> None:
    memory_id = call(executor, tool_context, "remember", content="Likes tea").result.data["memory"]["id"]
    outcome = call(executor, tool_context, "forget_memory", memory_id=memory_id)
    assert outcome.ok
    assert memory.get(memory_id) is None


def test_list_memories_never_shows_session_notes(executor: ToolExecutor, tool_context: ToolContext, memory) -> None:
    call(executor, tool_context, "remember", content="Studies ECE")
    memory.set_session_context("topic", "Thevenin")
    outcome = call(executor, tool_context, "list_memories")
    assert outcome.ok
    assert all(item["kind"] != "session" for item in outcome.result.data["memories"])


# ---------------------------------------------------------------------------
# productivity
# ---------------------------------------------------------------------------
def test_daily_briefing_tells_the_truth(executor: ToolExecutor, tool_context: ToolContext) -> None:
    empty = call(executor, tool_context, "daily_briefing")
    assert empty.ok, empty.message
    assert "briefing" in empty.result.summary.lower()
    assert "Calendar: not connected" in empty.result.summary, "it must not invent calendar events"

    call(executor, tool_context, "add_task", title="Submit assignment", due="today 17:00")
    call(executor, tool_context, "add_task", title="Overdue thing", due="yesterday")
    call(executor, tool_context, "set_reminder", title="Stand up", when="in 1 hour")
    briefing = call(executor, tool_context, "daily_briefing")
    assert briefing.ok
    assert "Overdue thing" in briefing.result.summary
    assert "Submit assignment" in briefing.result.summary
    assert "Stand up" in briefing.result.summary


def test_briefing_helper_matches_the_tool(
    executor: ToolExecutor, tool_context: ToolContext
) -> None:
    """``main.py --briefing`` and the window share this helper, so it must exist."""
    from lumi.tools.builtin.productivity import DailyBriefing, _briefing_text

    call(executor, tool_context, "add_task", title="Revise op-amps", due="today 19:00")

    text = _briefing_text(tool_context)
    tool = DailyBriefing().run({}, tool_context)
    assert tool.ok, tool.message
    assert text == tool.summary
    assert "Revise op-amps" in text
    assert "Calendar: not connected" in text

    # days_ahead is clamped rather than trusted.
    assert isinstance(_briefing_text(tool_context, days_ahead=999), str)


def test_focus_session_lifecycle(executor: ToolExecutor, tool_context: ToolContext, focus) -> None:
    started = call(executor, tool_context, "start_focus_session", minutes=25, label="Thevenin")
    assert started.ok, started.message
    assert focus.active() is not None

    twice = call(executor, tool_context, "start_focus_session", minutes=10)
    assert not twice.ok, "two focus sessions at once is not a thing"

    finished = call(executor, tool_context, "end_focus_session")
    assert finished.ok
    assert focus.active() is None

    stats = call(executor, tool_context, "focus_stats")
    assert stats.ok


def test_end_focus_session_without_one(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "end_focus_session")
    assert not outcome.ok


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
def test_write_then_read_then_search(executor: ToolExecutor, tool_context: ToolContext, workspace) -> None:
    allow_writes(tool_context)
    target = workspace / "notes" / "bjt.md"
    written = call(executor, tool_context, "write_text_file", path=str(target), content="# BJT\nA transistor.", overwrite=True)
    assert written.ok, written.message
    assert target.exists()

    read = call(executor, tool_context, "read_text_file", path=str(target))
    assert read.ok
    assert "transistor" in read.result.summary.lower()

    listing = call(executor, tool_context, "list_folder", path=str(workspace))
    assert listing.ok
    assert any(item["name"] == "notes" for item in listing.result.data["entries"])

    found = call(executor, tool_context, "find_files", name="*.md", folder=str(workspace))
    assert found.ok
    assert [item["name"] for item in found.result.data["files"]] == ["bjt.md"]

    searched = call(executor, tool_context, "search_in_files", query="transistor", folder=str(workspace))
    assert searched.ok
    assert searched.result.data["matches"]


def test_write_outside_the_workspace_is_refused(executor: ToolExecutor, tool_context: ToolContext, tmp_path) -> None:
    allow_writes(tool_context)
    outside = tmp_path.parent / "not-allowed" / "file.txt"
    outcome = call(executor, tool_context, "write_text_file", path=str(outside), content="nope", overwrite=True)
    assert not outcome.ok
    assert "workspace" in outcome.result.error.lower()
    assert not outside.exists()


def test_overwrite_asks_first(executor: ToolExecutor, tool_context: ToolContext, workspace) -> None:
    allow_writes(tool_context)
    target = workspace / "keep.txt"
    target.write_text("original", encoding="utf-8")
    outcome = declined(executor, tool_context, "write_text_file", path=str(target), content="changed", overwrite=True)
    assert outcome.status in {"declined", "denied"}, outcome.message
    assert target.read_text() == "original", "a declined overwrite must not touch the file"


def test_delete_files_needs_confirmation(executor: ToolExecutor, tool_context: ToolContext, workspace) -> None:
    allow_writes(tool_context)
    victim = workspace / "junk.txt"
    victim.write_text("junk", encoding="utf-8")
    outcome = declined(executor, tool_context, "delete_files", paths=[str(victim)])
    assert outcome.status == "declined"
    assert victim.exists()

    confirmed = call(executor, tool_context, "delete_files", paths=[str(victim)])
    assert confirmed.ok
    assert not victim.exists()


def test_plan_file_organisation_changes_nothing(executor: ToolExecutor, tool_context: ToolContext, workspace) -> None:
    allow_writes(tool_context)
    # Tidying a folder is a bulk operation, so the switch must be on first.
    tool_context.settings.automation.allow_bulk_file_operations = True
    (workspace / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    (workspace / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    plan = call(executor, tool_context, "plan_file_organisation", folder=str(workspace))
    assert plan.ok, plan.message
    assert (workspace / "photo.jpg").exists(), "planning must never move anything"
    assert not (workspace / "Images").exists()


def test_find_duplicates_reports_but_keeps_files(executor: ToolExecutor, tool_context: ToolContext, workspace) -> None:
    payload = b"same content" * 400  # clears the default 4 KB size floor
    (workspace / "a.bin").write_bytes(payload)
    (workspace / "b.bin").write_bytes(payload)
    outcome = call(executor, tool_context, "find_duplicates", folder=str(workspace))
    assert outcome.ok
    groups = outcome.result.data["groups"]
    assert groups and groups[0]["count"] == 2
    assert (workspace / "a.bin").exists() and (workspace / "b.bin").exists()


def test_move_files_within_the_workspace(executor: ToolExecutor, tool_context: ToolContext, workspace) -> None:
    allow_writes(tool_context)
    source = workspace / "loose.txt"
    source.write_text("move me", encoding="utf-8")
    destination = workspace / "sorted"
    outcome = call(executor, tool_context, "move_files", paths=[str(source)], destination=str(destination))
    assert outcome.ok, outcome.message
    assert (destination / "loose.txt").exists()
    assert not source.exists()


def test_read_document_says_what_it_needs(executor: ToolExecutor, tool_context: ToolContext, workspace) -> None:
    doc = workspace / "lecture.pdf"
    doc.write_bytes(b"%PDF-1.4 not really a pdf")
    outcome = call(executor, tool_context, "read_document", path=str(doc))
    # Either it extracts text (pypdf installed) or it explains, clearly, why not.
    message = (outcome.result.hint + outcome.result.error).lower()
    assert outcome.ok or "pypdf" in message or "ocr" in message


# ---------------------------------------------------------------------------
# computer control: unavailable here, and refused unless the user allows it
# ---------------------------------------------------------------------------
def test_open_application_is_denied_by_default(executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_app_launch = False
    outcome = call(executor, tool_context, "open_application", name="notepad")
    assert outcome.status == "denied"
    assert "allow_app_launch" in outcome.message


def test_unknown_application_gives_suggestions(executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_app_launch = True
    outcome = call(executor, tool_context, "open_application", name="photoshoop")
    assert not outcome.ok
    assert "photoshoop" in outcome.result.error
    assert outcome.result.hint


def test_screenshot_and_typing_are_denied_by_default(executor: ToolExecutor, tool_context: ToolContext) -> None:
    screenshot = call(executor, tool_context, "take_screenshot")
    assert screenshot.status == "denied"
    assert "allow_screenshots" in screenshot.message

    typing = call(executor, tool_context, "type_text", text="hello")
    assert typing.status == "denied"
    assert "allow_keyboard_mouse" in typing.message


def test_open_path_refuses_outside_allowed_folders(executor: ToolExecutor, tool_context: ToolContext, tmp_path) -> None:
    tool_context.settings.automation.allow_app_launch = True
    outside = tmp_path.parent / "secret-folder"
    outcome = call(executor, tool_context, "open_path", target=str(outside))
    assert not outcome.ok


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------
def test_search_web_is_denied_by_default(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "search_web", query="thevenin theorem")
    assert outcome.status == "denied"
    assert "allow_browser_automation" in outcome.message


def test_open_website_refuses_non_http_urls(executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_browser_automation = True
    outcome = call(executor, tool_context, "open_website", url="file:///etc/passwd")
    assert not outcome.ok
    assert "http" in outcome.result.error.lower()


def test_compose_email_never_sends(executor: ToolExecutor, tool_context: ToolContext) -> None:
    import shutil

    if not any(shutil.which(launcher) for launcher in ("xdg-open", "open", "start")):
        pytest.skip("no browser launcher in this environment")
    tool_context.settings.automation.allow_browser_automation = True
    outcome = call(executor, tool_context, "compose_email", to="prof@college.edu", subject="Lab record", body="Attached.")
    assert outcome.ok, outcome.message
    text = outcome.result.summary.lower()
    assert "send" not in text or "not" in text or "draft" in text
    assert outcome.result.verified is False or outcome.result.verified is None


def test_send_email_requires_confirmation(executor: ToolExecutor, tool_context: ToolContext, audit) -> None:
    tool_context.settings.automation.allow_browser_automation = True
    outcome = declined(executor, tool_context, "send_email", to="prof@college.edu", subject="x", body="y")
    assert outcome.status == "declined"
    statuses = [entry.status for entry in audit.recent(limit=20) if entry.tool == "send_email"]
    assert "denied" in statuses


def test_fill_web_field_refuses_password_fields(executor: ToolExecutor, tool_context: ToolContext) -> None:
    tool_context.settings.automation.allow_browser_automation = True
    outcome = call(executor, tool_context, "fill_web_field", label="Password", value="hunter2")
    assert not outcome.ok
    assert "password" in outcome.result.error.lower()


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------
def test_ollama_status_without_a_server(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "ollama_status")
    assert outcome.ok, "a missing Ollama is a fact to report, not an error"
    assert outcome.result.data["status"]["running"] is False
    assert "not reachable" in outcome.result.summary
    assert "still handles tasks" in outcome.result.summary, "say what still works"


def test_list_local_models_without_a_server_explains_itself(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "list_local_models")
    assert not outcome.ok
    text = (outcome.result.summary + outcome.result.hint).lower()
    assert "ollama" in text and ("start it" in text or "not running" in text or "could not reach" in text)


def test_recommend_models_is_honest_about_this_machine(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "recommend_models")
    assert outcome.ok
    assert outcome.result.data["recommendations"]
    assert "GB" in outcome.result.summary


def test_system_health_reports_basics(executor: ToolExecutor, tool_context: ToolContext) -> None:
    outcome = call(executor, tool_context, "system_health")
    assert outcome.ok
    assert "cpu_percent" in outcome.result.data["load"]


def test_read_activity_shows_audit_log(executor: ToolExecutor, tool_context: ToolContext) -> None:
    call(executor, tool_context, "add_task", title="Audited task")
    outcome = call(executor, tool_context, "read_activity", limit=20)
    assert outcome.ok
    assert any(entry["tool"] == "add_task" for entry in outcome.result.data["entries"])


def test_read_and_clear_logs(executor: ToolExecutor, tool_context: ToolContext) -> None:
    logs = call(executor, tool_context, "read_logs", lines=10)
    assert logs.ok
    assert "log" in logs.result.summary.lower()

    cleared = call(executor, tool_context, "clear_logs")
    assert cleared.ok


# ---------------------------------------------------------------------------
# the executor never lets a bad call through, whatever the tool
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("arguments", [{}, {"path": "~/", "text": "x", "name": "nope", "query": "q", "title": "t"}])
def test_every_tool_survives_a_call(
    executor: ToolExecutor,
    tool_context: ToolContext,
    registry,
    arguments: dict,
) -> None:
    for tool in registry.all():
        outcome = executor.execute(
            ToolCall(tool.name, dict(arguments), source="test"),
            tool_context,
            confirmation_provider=lambda request: True,
        )
        assert outcome.status in {"ok", "invalid", "denied", "declined", "error", "cancelled", "timeout", "unavailable"}, tool.name
        assert outcome.result.summary, tool.name
        assert "Traceback" not in (outcome.result.error or ""), tool.name
        assert "Traceback" not in (outcome.message or ""), tool.name
