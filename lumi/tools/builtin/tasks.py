"""Task tools: create, list, update, complete, postpone, delete.

All of them are thin wrappers over :class:`lumi.tasks.repository.TaskRepository`.
Date parsing is deterministic (:mod:`lumi.tasks.nlp`), so "tomorrow at 6 pm" is
understood identically whether the model is running or not - and the assistant
shows the interpretation before saving anything ambiguous.
"""

from __future__ import annotations

from typing import Any, Iterable

from lumi.tasks import nlp
from lumi.tasks.models import PRIORITIES
from lumi.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult, ConfirmationRequest
from lumi.tools.builtin._common import (
    SERVICE_TASKS,
    bool_arg,
    int_arg,
    ok,
    parse_when,
    resolve_task,
    service,
    string_list,
    task_list_text,
    task_payload,
)
from lumi.utils.timeutil import humanise_due, now as current_time, to_iso

TASK_ID_SCHEMA = {"type": "integer", "description": "The task number shown as #12 in the task list."}
TASK_HINT_SCHEMA = {"type": "string", "description": "Part of the task title, if the number is unknown."}
WHEN_SCHEMA = {
    "type": "string",
    "description": (
        "When it is due. Natural language is fine: 'tomorrow 6pm', 'in 2 hours', "
        "'next Monday', '12 Oct 18:00'. Leave empty for no deadline."
    ),
}


class AddTaskFromText(Tool):
    name = "add_task_from_text"
    description = (
        "Create a task or reminder from a plain-English sentence. Use this whenever the user says "
        "things like 'remind me to submit the lab record tomorrow at 6 pm'. The date and time are "
        "parsed locally and confirmed with the user when they are ambiguous."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The user's sentence, verbatim."},
            "category": {"type": "string", "description": "Optional category such as college, personal, coding, projects."},
            "priority": {"type": "string", "enum": list(PRIORITIES), "description": "Optional priority."},
        },
        "required": ["text"],
    }
    category = "tasks"
    risk = RiskLevel.MODERATE
    timeout_s = 15.0
    examples = ("remind me to call mom at 7pm", "add task: revise Thevenin tomorrow")

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        text = str(arguments.get("text", "")).strip()
        if not text:
            raise ToolError("I need the sentence to turn into a task.")
        parsed = nlp.parse_task_text(text)
        if not parsed.title:
            raise ToolError(
                "I could not find a task description in that sentence.",
                hint="Try: 'remind me to submit the lab record tomorrow at 6 pm'.",
            )
        if parsed.needs_confirmation:
            question = nlp.describe_ambiguity(parsed)
            approved = context.request_confirmation(
                ConfirmationRequest(
                    tool=self.name,
                    title="Check the date and time",
                    details=question,
                    risk=RiskLevel.MODERATE,
                    arguments={"text": text},
                    preview="\n".join(f"{key}: {value}" for key, value in parsed.to_dict().items() if value),
                    allow_session_grant=False,
                )
            )
            if not approved:
                return ToolResult.failure(
                    "You did not confirm the date, so I did not save anything.",
                    error="not confirmed",
                    hint="Tell me the exact date and time and I will add it.",
                )

        tasks = service(context, SERVICE_TASKS)
        category = str(arguments.get("category") or parsed.category or context.settings.productivity.default_category)
        priority = str(arguments.get("priority") or parsed.priority or context.settings.productivity.default_priority)

        task = tasks.create(
            parsed.title,
            notes="",
            category=category,
            priority=priority if priority in PRIORITIES else "medium",
            due_at=parsed.due_at,
            remind_at=parsed.remind_at,
            recurrence=parsed.recurrence,
            tags=parsed.tags,
            source="chat",
        )

        reminders = context.services.get("reminders")
        reminder_note = ""
        if reminders is not None and parsed.remind_at and task.remind_at:
            try:
                reminder = reminders.repo.create(
                    task.title,
                    task.remind_at,
                    body=f"Task #{task.id}",
                    recurrence=parsed.recurrence,
                    task_id=task.id,
                )
                reminder_note = f" I will notify you at {humanise_due(reminder.fire_at)}."
            except Exception:  # pragma: no cover - reminder creation is a bonus
                reminder_note = " (I could not schedule the pop-up reminder.)"

        interpretation = parsed.describe()
        summary = f"Added task #{task.id}: {task.title} — {humanise_due(task.due_at)}"
        if parsed.assumptions:
            summary += " (assumptions: " + "; ".join(parsed.assumptions) + ")"
        return ok(
            summary + reminder_note,
            data={"task": task_payload(task), "interpretation": interpretation, "assumptions": parsed.assumptions},
            verified=True,
        )


class AddTask(Tool):
    name = "add_task"
    description = (
        "Create a task with explicit fields (title, due date, priority, category). "
        "Use this when the details are already clear rather than in a sentence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What needs doing.", "maxLength": 300},
            "due": WHEN_SCHEMA,
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "category": {"type": "string", "description": "college, personal, coding, projects, ..."},
            "notes": {"type": "string", "description": "Optional extra detail.", "maxLength": 4000},
            "recurrence": {"type": "string", "description": "none, daily, weekly, weekdays, monthly, 'every 3 days'."},
            "remind_minutes_before": {"type": "integer", "minimum": 0, "maximum": 20160, "description": "Schedule a reminder this many minutes before the due time."},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        },
        "required": ["title"],
    }
    category = "tasks"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = service(context, SERVICE_TASKS)
        due_text = str(arguments.get("due", "")).strip()
        due = parse_when(due_text) if due_text else None
        if due_text and due is None:
            raise ToolError(
                f"I could not understand the date '{due_text}'.",
                hint="Use something like 'tomorrow 6pm' or '2026-10-12 18:00'.",
            )
        remind_at = None
        minutes = int_arg(arguments, "remind_minutes_before", 0)
        if due is not None and minutes > 0:
            from datetime import timedelta

            remind_at = due - timedelta(minutes=minutes)

        task = tasks.create(
            str(arguments.get("title", "")),
            notes=str(arguments.get("notes", "")),
            category=str(arguments.get("category") or context.settings.productivity.default_category),
            priority=str(arguments.get("priority") or context.settings.productivity.default_priority),
            due_at=due,
            remind_at=remind_at,
            recurrence=str(arguments.get("recurrence") or "none"),
            tags=string_list(arguments.get("tags")),
            source="chat",
        )
        reminders = context.services.get("reminders")
        if reminders is not None and remind_at is not None:
            try:
                reminders.repo.create(task.title, remind_at, body=f"Task #{task.id}", task_id=task.id)
            except Exception:  # pragma: no cover
                pass
        return ok(
            f"Added task #{task.id}: {task.title} — {humanise_due(task.due_at)}",
            data={"task": task_payload(task)},
            verified=True,
        )


class ListTasks(Tool):
    name = "list_tasks"
    description = (
        "List the user's tasks. Use it to answer 'what do I have to do today', to find a task's "
        "#number, or to check something is still open."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["today", "tomorrow", "overdue", "upcoming", "open", "completed", "all"],
                "description": "Which slice of the task list to show. Default: open.",
            },
            "category": {"type": "string"},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "search": {"type": "string", "description": "Only tasks containing this text."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    }
    category = "tasks"
    risk = RiskLevel.SAFE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from lumi.utils.timeutil import end_of_day, start_of_day

        tasks = service(context, SERVICE_TASKS)
        scope = str(arguments.get("scope") or "open").lower()
        limit = int_arg(arguments, "limit", 25)
        category = arguments.get("category")
        priority = arguments.get("priority")
        search = arguments.get("search")
        reference = current_time()

        if scope == "today":
            items = tasks.due_today(reference=reference)
        elif scope == "tomorrow":
            from datetime import timedelta

            items = tasks.due_between(start_of_day(reference + timedelta(days=1)), end_of_day(reference + timedelta(days=1)))
        elif scope == "overdue":
            items = tasks.overdue(reference=reference)
        elif scope == "upcoming":
            items = tasks.upcoming(days=7, reference=reference)
        elif scope in {"completed", "all"}:
            items = tasks.list(status="done" if scope == "completed" else "all", category=category, priority=priority, search=search, limit=limit)
        else:
            items = tasks.list(status="open", category=category, priority=priority, search=search, limit=limit)

        if scope in {"today", "tomorrow", "overdue", "upcoming"} and (category or priority or search):
            filtered = []
            for task in items:
                if category and task.category.lower() != str(category).lower():
                    continue
                if priority and task.priority != priority:
                    continue
                if search and str(search).lower() not in (task.title + " " + task.notes).lower():
                    continue
                filtered.append(task)
            items = filtered

        text = task_list_text(items, reference=reference, limit=limit)
        stats = tasks.stats(reference=reference)
        headline = {
            "today": f"You have {len(items)} task(s) due today.",
            "tomorrow": f"{len(items)} task(s) due tomorrow.",
            "overdue": f"{len(items)} overdue task(s).",
            "upcoming": f"{len(items)} task(s) in the next 7 days.",
        }.get(scope, f"{len(items)} task(s).")
        if stats["overdue"] and scope not in {"overdue"}:
            headline += f" {stats['overdue']} are overdue."
        return ok(
            headline + "\n\n" + text,
            data={"tasks": [task_payload(task, reference=reference) for task in items[:limit]], "text": text, "stats": stats},
            verified=True,
        )


class UpdateTask(Tool):
    name = "update_task"
    description = "Change a task: rename it, move the deadline, change priority, category, notes or repeat rule."
    parameters = {
        "type": "object",
        "properties": {
            "task_id": TASK_ID_SCHEMA,
            "task": TASK_HINT_SCHEMA,
            "title": {"type": "string", "maxLength": 300},
            "due": WHEN_SCHEMA,
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "category": {"type": "string"},
            "notes": {"type": "string", "maxLength": 4000},
            "recurrence": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done", "cancelled"]},
        },
    }
    category = "tasks"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = service(context, SERVICE_TASKS)
        task = resolve_task(context, arguments)
        changes: dict[str, Any] = {}
        if arguments.get("title"):
            changes["title"] = str(arguments["title"])
        if arguments.get("due"):
            when = parse_when(str(arguments["due"]))
            if when is None:
                raise ToolError(f"I could not understand the date '{arguments['due']}'.")
            changes["due_at"] = when
        if arguments.get("priority"):
            changes["priority"] = str(arguments["priority"])
        if arguments.get("category"):
            changes["category"] = str(arguments["category"])
        if "notes" in arguments:
            changes["notes"] = str(arguments.get("notes") or "")
        if arguments.get("recurrence"):
            changes["recurrence"] = str(arguments["recurrence"])
        if arguments.get("status"):
            changes["status"] = str(arguments["status"])
        if not changes:
            return ok(f"Nothing to change on #{task.id} {task.title}.", data={"task": task_payload(task)})

        updated = tasks.update(int(task.id), **changes)
        if updated is None:  # pragma: no cover - defensive
            raise ToolError(f"Task #{task.id} could not be updated.")
        changed = ", ".join(sorted(changes))
        return ok(
            f"Updated #{updated.id} {updated.title}: {changed}. Now due {humanise_due(updated.due_at)}.",
            data={"task": task_payload(updated), "changed": sorted(changes)},
            verified=True,
        )


class CompleteTask(Tool):
    name = "complete_task"
    description = "Mark a task as done (or put it back to pending)."
    parameters = {
        "type": "object",
        "properties": {
            "task_id": TASK_ID_SCHEMA,
            "task": TASK_HINT_SCHEMA,
            "undo": {"type": "boolean", "description": "Set true to reopen the task instead of completing it."},
        },
    }
    category = "tasks"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = service(context, SERVICE_TASKS)
        task = resolve_task(context, arguments)
        undo = bool_arg(arguments, "undo", False)
        updated = tasks.complete(int(task.id), completed=not undo)
        if updated is None:  # pragma: no cover
            raise ToolError(f"Task #{task.id} could not be updated.")
        if undo:
            return ok(f"Reopened #{updated.id} {updated.title}.", data={"task": task_payload(updated)}, verified=True)
        message = f"Done: #{updated.id} {updated.title}."
        if updated.recurrence not in ("", "none"):
            follow_up = tasks.list(status="open", search=updated.title, limit=3)
            upcoming = [item for item in follow_up if item.id != updated.id]
            if upcoming:
                message += f" The next repeat is #{upcoming[0].id} due {humanise_due(upcoming[0].due_at)}."
        return ok(message, data={"task": task_payload(updated)}, verified=True)


class PostponeTask(Tool):
    name = "postpone_task"
    description = "Move a task's deadline later (by a number of days, or to a new date/time)."
    parameters = {
        "type": "object",
        "properties": {
            "task_id": TASK_ID_SCHEMA,
            "task": TASK_HINT_SCHEMA,
            "when": {"type": "string", "description": "New due date/time in natural language."},
            "days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "Push it this many days from its current deadline."},
            "minutes": {"type": "integer", "minimum": 5, "maximum": 10080, "description": "Push it this many minutes."},
        },
    }
    category = "tasks"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = service(context, SERVICE_TASKS)
        task = resolve_task(context, arguments)
        when_text = str(arguments.get("when") or "").strip()
        if when_text:
            when = parse_when(when_text)
            if when is None:
                raise ToolError(
                    f"I could not understand '{when_text}' as a new date.",
                    hint="Try 'tomorrow at 6 pm' or 'in 3 days'.",
                )
            updated = tasks.postpone(int(task.id), until=when)
        elif arguments.get("days"):
            updated = tasks.postpone(int(task.id), days=int_arg(arguments, "days", 1))
        elif arguments.get("minutes"):
            updated = tasks.postpone(int(task.id), minutes=int_arg(arguments, "minutes", 30))
        else:
            updated = tasks.postpone(int(task.id), days=1)
        if updated is None:  # pragma: no cover
            raise ToolError(f"Task #{task.id} could not be postponed.")
        return ok(
            f"Moved #{updated.id} {updated.title} to {humanise_due(updated.due_at)}.",
            data={"task": task_payload(updated)},
            verified=True,
        )


class DeleteTask(Tool):
    name = "delete_task"
    description = "Delete a task permanently. Always asks for confirmation first."
    parameters = {
        "type": "object",
        "properties": {"task_id": TASK_ID_SCHEMA, "task": TASK_HINT_SCHEMA},
    }
    category = "tasks"
    risk = RiskLevel.CONSEQUENTIAL
    always_confirm = True
    confirmation_title = "Delete this task?"
    permission_action = "delete_task"
    timeout_s = 10.0

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        request = super().confirmation_details(arguments, context)
        try:
            task = resolve_task(context, arguments)
            request.details = f"Task #{task.id}: {task.title}"
            request.preview = (
                f"Due: {humanise_due(task.due_at)}\n"
                f"Category: {task.category} · Priority: {task.priority}\n"
                "This removes the task from your list for good."
            )
        except ToolError:
            request.details = "Delete the requested task."
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = service(context, SERVICE_TASKS)
        task = resolve_task(context, arguments)
        title = task.title
        task_id = task.id
        deleted = tasks.delete(int(task_id))
        if not deleted:  # pragma: no cover
            raise ToolError(f"Task #{task_id} was already gone.")
        return ok(f"Deleted #{task_id} {title}.", data={"task_id": task_id, "title": title}, verified=True)


class TaskStats(Tool):
    name = "task_stats"
    description = "Quick counts: open tasks, due today, overdue and completed today."
    parameters = {"type": "object", "properties": {}}
    category = "tasks"
    risk = RiskLevel.SAFE
    timeout_s = 5.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = service(context, SERVICE_TASKS)
        stats = tasks.stats()
        summary = (
            f"{stats['open']} open, {stats['due_today']} due today, "
            f"{stats['overdue']} overdue, {stats['done_today']} completed today."
        )
        return ok(summary, data={"stats": stats}, verified=True)


class SuggestTaskOrder(Tool):
    name = "suggest_task_order"
    description = (
        "Suggest a sensible order for today's work, based on deadlines and priorities. "
        "Deterministic: deadlines first, then priority - it does not guess."
    )
    parameters = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
    }
    category = "tasks"
    risk = RiskLevel.SAFE
    timeout_s = 5.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        tasks = service(context, SERVICE_TASKS)
        reference = current_time()
        ordered = tasks.suggest_order(reference=reference, limit=int_arg(arguments, "limit", 8))
        if not ordered:
            return ok("Nothing is pending, so there is no order to suggest.", data={"tasks": []}, verified=True)
        lines = []
        for index, task in enumerate(ordered, start=1):
            reason = "overdue" if task.is_overdue(reference) else ("due " + humanise_due(task.due_at, reference=reference))
            lines.append(f"{index}. #{task.id} {task.title} ({reason}, {task.priority} priority)")
        return ok(
            "Suggested order (deadline first, then priority):\n" + "\n".join(lines),
            data={"tasks": [task_payload(task, reference=reference) for task in ordered], "text": "\n".join(lines)},
            verified=True,
        )


def build_tools() -> Iterable[Tool]:
    return (
        AddTaskFromText(),
        AddTask(),
        ListTasks(),
        UpdateTask(),
        CompleteTask(),
        PostponeTask(),
        DeleteTask(),
        TaskStats(),
        SuggestTaskOrder(),
    )
