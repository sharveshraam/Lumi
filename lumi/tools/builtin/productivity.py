"""Daily briefing and focus sessions.

Both are deterministic: they report what is actually in the database and never
invent a deadline, an event or a completed task.  If calendar integration is not
configured, the briefing says so instead of guessing.
"""

from __future__ import annotations

from typing import Any, Iterable

from lumi.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from lumi.tools.builtin._common import (
    SERVICE_CONVERSATIONS,
    SERVICE_FOCUS,
    SERVICE_NOTES,
    SERVICE_REMINDERS,
    SERVICE_SCHEDULER,
    SERVICE_TASKS,
    bool_arg,
    int_arg,
    ok,
    service,
    task_list_text,
)
from lumi.utils.timeutil import humanise_delta, humanise_due, now as current_time, start_of_day, to_iso


def _briefing_text(
    context: ToolContext,
    *,
    include_completed: bool = False,
    days_ahead: int = 7,
    reference: Any = None,
) -> str:
    """Build the briefing text.  Shared by the tool, the CLI and the window."""
    reference = reference or current_time()
    tasks = service(context, SERVICE_TASKS)
    days_ahead = max(1, min(30, int(days_ahead)))

            
    due_today = tasks.due_today(reference=reference)
    overdue = [task for task in tasks.overdue(reference=reference) if task.id not in {item.id for item in due_today}]
    upcoming = tasks.upcoming(days=days_ahead, reference=reference)
    suggested = tasks.suggest_order(reference=reference, limit=8)
    
    if overdue:
        order = [*overdue, *due_today]
    else:
        order = due_today or upcoming

    sections: list[str] = []
    header = reference.strftime("%A %d %B %Y · %H:%M")
    sections.append(f"# Briefing — {header}")

    sections.append(
        f"## Today\n"
        + (
            task_list_text(order, reference=reference, limit=12)
            if order
            else "Nothing is due today and nothing is overdue. Good time for revision or project work."
        )
    )

    if overdue:
        sections.append("## Overdue\n" + task_list_text(overdue, reference=reference, limit=8))

    later = [task for task in upcoming if task.id not in {item.id for item in due_today}]
    if later:
        sections.append(f"## Next {days_ahead} days\n" + task_list_text(later, reference=reference, limit=10))

    reminders = context.services.get(SERVICE_REMINDERS)
    if reminders is not None:
        upcoming_reminders = reminders.repo.upcoming(limit=6)
        if upcoming_reminders:
            lines = [
                f"#{item.id} {item.title} — {humanise_due(item.fire_at)}"
                for item in upcoming_reminders
            ]
            sections.append("## Reminders\n" + "\n".join(lines))

    notes = context.services.get(SERVICE_NOTES)
    if notes is not None:
        pinned = notes.pinned(limit=5)
        if pinned:
            lines = [f"#{note.id} {note.title}: {note.excerpt(length=120)}" for note in pinned]
            sections.append("## Pinned notes\n" + "\n".join(lines))

    focus = context.services.get(SERVICE_FOCUS)
    focus_line = ""
    if focus is not None:
        minutes = focus.today_minutes()
        week = focus.week_stats(days=7)
        focus_line = (
            f"Focus today: {minutes:.0f} minutes. "
            f"Last 7 days: {week['sessions']} sessions, {week['total_minutes']:.0f} minutes total."
        )

    if suggested:
        lines = []
        for index, task in enumerate(suggested[:6], start=1):
            reason = "overdue" if task.is_overdue(reference) else f"due {humanise_due(task.due_at, reference=reference)}"
            lines.append(f"{index}. #{task.id} {task.title} ({reason}, {task.priority} priority)")
        sections.append("## Suggested order\n" + "\n".join(lines))

    calendar_note = (
        "Calendar: not connected. LUMI does not read your calendar unless you set up a local "
        "integration in Settings → Integrations."
    )
    sections.append("## Notes from LUMI\n" + calendar_note + (("\n" + focus_line) if focus_line else ""))

    if include_completed:
        completed = tasks.list(status="done", limit=10)
        today_done = [task for task in completed if task.completed_at and task.completed_at >= start_of_day(reference)]
        if today_done:
            sections.append(
                "## Completed today\n"
                + "\n".join(f"✓ #{task.id} {task.title}" for task in today_done[:10])
            )

    return "\n\n".join(sections)


class DailyBriefing(Tool):
    name = "daily_briefing"
    description = (
        "Build the user's briefing from their own local data: today's tasks, overdue items, "
        "upcoming deadlines, reminders, pinned notes and a suggested order of work. "
        "Everything comes from the database - nothing here is invented."
    )
    parameters = {
        "type": "object",
        "properties": {
            "include_completed": {"type": "boolean", "description": "Also list what was completed today."},
            "days_ahead": {"type": "integer", "minimum": 1, "maximum": 30, "description": "How far ahead to look for deadlines (default 7)."},
        },
    }
    category = "productivity"
    risk = RiskLevel.SAFE
    timeout_s = 15.0
    examples = ("give me my briefing", "what does my day look like?")

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        include_completed = bool_arg(arguments, "include_completed", False)
        days_ahead = int_arg(arguments, "days_ahead", 7)
        reference = current_time()
        tasks = service(context, SERVICE_TASKS)

        briefing = _briefing_text(
            context,
            include_completed=include_completed,
            days_ahead=days_ahead,
            reference=reference,
        )

        due_today = tasks.due_today(reference=reference)
        due_ids = {task.id for task in due_today}
        overdue = [
            task for task in tasks.overdue(reference=reference) if task.id not in due_ids
        ]
        return ok(
            briefing,
            data={
                "stats": tasks.stats(reference=reference),
                "due_today": [task.id for task in due_today],
                "overdue": [task.id for task in overdue],
                "generated_at": to_iso(reference),
            },
            verified=True,
        )


class StartFocusSession(Tool):
    name = "start_focus_session"
    description = (
        "Start a timed focus (study) session, optionally linked to a task. Records the session "
        "locally. LUMI does not block applications or hide anything."
    )
    parameters = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "maxLength": 200, "description": "What the session is for."},
            "task_id": {"type": "integer", "description": "Optional task number to attach it to."},
            "minutes": {"type": "integer", "minimum": 5, "maximum": 180, "description": "Planned length (default from settings, usually 25)."},
        },
    }
    category = "productivity"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        focus = service(context, SERVICE_FOCUS)
        active = focus.active()
        if active is not None:
            raise ToolError(
                f"A focus session is already running ({active.label or 'current session'}).",
                hint="Finish it first with end_focus_session.",
            )
        label = str(arguments.get("label", "")).strip()
        task_id = arguments.get("task_id")
        if task_id and not label:
            tasks = context.services.get(SERVICE_TASKS)
            if tasks is not None:
                task = tasks.get(int(task_id))
                if task is not None:
                    label = task.title
        minutes = int_arg(arguments, "minutes", int(context.settings.notifications.focus_length_minutes))
        session = focus.start(label=label, task_id=int(task_id) if task_id else None, planned_minutes=minutes)
        return ok(
            f"Focus session started: {session.label or 'study'} for {minutes} minutes. I will stay out of the way.",
            data={"session": session.to_dict()},
            verified=True,
        )


class EndFocusSession(Tool):
    name = "end_focus_session"
    description = "Finish the running focus session and record how long it lasted."
    parameters = {
        "type": "object",
        "properties": {
            "completed": {"type": "boolean", "description": "True if the goal was reached (default true)."},
            "notes": {"type": "string", "maxLength": 1000},
        },
    }
    category = "productivity"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        focus = service(context, SERVICE_FOCUS)
        active = focus.active()
        if active is None:
            raise ToolError("There is no focus session running.")
        session = focus.finish(
            int(active.id), completed=bool_arg(arguments, "completed", True), notes=str(arguments.get("notes", ""))
        )
        assert session is not None
        return ok(
            f"Logged {session.actual_minutes:.0f} minutes of focus on '{session.label or 'study'}'.",
            data={"session": session.to_dict()},
            verified=True,
        )


class FocusStats(Tool):
    name = "focus_stats"
    description = "How much focused study time was recorded recently."
    parameters = {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90}},
    }
    category = "productivity"
    risk = RiskLevel.SAFE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        focus = service(context, SERVICE_FOCUS)
        days = int_arg(arguments, "days", 7)
        stats = focus.week_stats(days=days)
        by_label = ", ".join(f"{label}: {minutes:.0f} min" for label, minutes in stats["by_label"].items()) or "no sessions"
        summary = (
            f"Last {days} days: {stats['sessions']} session(s), {stats['total_minutes']:.0f} minutes total "
            f"(average {stats['average_minutes']:.0f} minutes). {by_label}."
        )
        return ok(summary, data=stats, verified=True)


def build_tools() -> Iterable[Tool]:
    return (DailyBriefing(), StartFocusSession(), EndFocusSession(), FocusStats())
