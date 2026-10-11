"""Shared helpers for built-in tools.

Keeping the repetitive parts (argument coercion, service lookup, date parsing,
task resolution) here means each tool file contains only what it actually does.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from lumi.tasks import nlp
from lumi.tasks.models import Task
from lumi.tools.base import ToolContext, ToolError, ToolResult
from lumi.utils.timeutil import humanise_due, now as current_time, to_iso

SERVICE_TASKS = "tasks"
SERVICE_NOTES = "notes"
SERVICE_REMINDERS = "reminders"
SERVICE_MEMORY = "memory"
SERVICE_FILES = "files"
SERVICE_AUTOMATION = "automation"
SERVICE_BROWSER = "browser"
SERVICE_FOCUS = "focus"
SERVICE_CONVERSATIONS = "conversations"
SERVICE_ENGINE = "engine"
SERVICE_SCHEDULER = "scheduler"
SERVICE_SETTINGS_STORE = "settings_store"


def service(context: ToolContext, name: str) -> Any:
    """Fetch a service, raising a friendly :class:`ToolError` when missing."""
    try:
        return context.service(name)
    except ToolError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise ToolError(f"The {name} service is unavailable ({type(exc).__name__}).") from exc


def optional_service(context: ToolContext, name: str) -> Optional[Any]:
    return context.services.get(name)


def parse_when(text: str, *, reference: datetime | None = None) -> Optional[datetime]:
    """Parse a date/time phrase such as ``"tomorrow 6pm"`` or ``"2026-10-12 18:00"``."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        iso = datetime.fromisoformat(raw)
        return iso.replace(microsecond=0)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M", "%Y/%m/%d %H:%M", "%d %b %Y %H:%M", "%d %B %Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    parsed = nlp.parse_task_text(raw, reference=reference)
    return parsed.due_at or parsed.remind_at


def require_when(text: str, *, field: str = "when") -> datetime:
    value = parse_when(text)
    if value is None:
        raise ToolError(
            f"I could not understand the {field} '{text}'.",
            hint="Try something like 'tomorrow at 6 pm', 'in 2 hours' or '12 Oct 18:00'.",
        )
    return value


def resolve_task(context: ToolContext, arguments: dict[str, Any]) -> Task:
    """Find a task by id or by a title hint; raise a helpful error otherwise."""
    tasks = service(context, SERVICE_TASKS)
    task_id = arguments.get("task_id")
    hint = str(arguments.get("task") or arguments.get("title") or "").strip()

    if task_id not in (None, "", 0):
        try:
            numeric = int(task_id)
        except (TypeError, ValueError):
            raise ToolError(f"'{task_id}' is not a task number.", hint="Task numbers look like #12.")
        task = tasks.get(numeric)
        if task is None:
            raise ToolError(f"There is no task #{numeric}.", hint="Ask me to list your tasks to see the numbers.")
        return task

    if hint:
        matches = tasks.find_by_title(hint) or tasks.link_ambiguous_titles(hint)
        if not matches:
            raise ToolError(
                f"I could not find an open task matching '{hint}'.",
                hint="Ask me to list your tasks, then use the #number.",
            )
        if len(matches) > 1:
            options = ", ".join(f"#{match.id} {match.title}" for match in matches[:4])
            raise ToolError(
                f"'{hint}' matches more than one task: {options}.",
                hint="Tell me the #number of the one you mean.",
            )
        return matches[0]

    raise ToolError("Tell me which task you mean (its number, or part of its title).")


def resolve_reminder(context: ToolContext, arguments: dict[str, Any]) -> Any:
    reminders = service(context, SERVICE_REMINDERS)
    reminder_id = arguments.get("reminder_id")
    hint = str(arguments.get("title") or arguments.get("reminder") or "").strip()
    repo = reminders.repo
    if reminder_id not in (None, "", 0):
        try:
            numeric = int(reminder_id)
        except (TypeError, ValueError):
            raise ToolError(f"'{reminder_id}' is not a reminder number.")
        reminder = repo.get(numeric)
        if reminder is None:
            raise ToolError(f"There is no reminder #{numeric}.")
        return reminder
    if hint:
        candidates = [item for item in repo.list(limit=200) if hint.lower() in item.title.lower()]
        if not candidates:
            raise ToolError(f"I could not find a reminder matching '{hint}'.")
        if len(candidates) > 1:
            options = ", ".join(f"#{item.id} {item.title}" for item in candidates[:4])
            raise ToolError(f"'{hint}' matches several reminders: {options}.")
        return candidates[0]
    raise ToolError("Tell me which reminder you mean.")


def resolve_note(context: ToolContext, arguments: dict[str, Any]) -> Any:
    notes = service(context, SERVICE_NOTES)
    note_id = arguments.get("note_id")
    hint = str(arguments.get("title") or arguments.get("query") or "").strip()
    if note_id not in (None, "", 0):
        try:
            numeric = int(note_id)
        except (TypeError, ValueError):
            raise ToolError(f"'{note_id}' is not a note number.")
        note = notes.get(numeric)
        if note is None:
            raise ToolError(f"There is no note #{numeric}.")
        return note
    if hint:
        matches = notes.find_by_title(hint)
        if not matches:
            matches = notes.search(hint)
        if not matches:
            raise ToolError(f"I could not find a note matching '{hint}'.")
        if len(matches) > 1:
            options = ", ".join(f"#{note.id} {note.title}" for note in matches[:4])
            raise ToolError(f"'{hint}' matches several notes: {options}.")
        return matches[0]
    raise ToolError("Tell me which note you mean.")


def task_payload(task: Task, *, reference: datetime | None = None) -> dict[str, Any]:
    ref = reference or current_time()
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "category": task.category,
        "due_at": to_iso(task.due_at),
        "due_human": humanise_due(task.due_at, reference=ref),
        "overdue": task.is_overdue(ref),
        "remind_at": to_iso(task.remind_at),
        "recurrence": task.recurrence,
        "tags": list(task.tags),
        "notes": task.notes[:500],
    }


def task_list_text(tasks: Sequence[Task], *, reference: datetime | None = None, limit: int = 20) -> str:
    ref = reference or current_time()
    if not tasks:
        return "No tasks to show."
    lines: list[str] = []
    for task in list(tasks)[:limit]:
        flags = []
        if task.is_overdue(ref):
            flags.append("OVERDUE")
        if task.priority in {"high", "urgent"}:
            flags.append(task.priority.upper())
        if task.recurrence not in ("", "none"):
            flags.append("repeats")
        suffix = f"  [{' · '.join(flags)}]" if flags else ""
        lines.append(f"#{task.id} {task.title} — {humanise_due(task.due_at, reference=ref)}{suffix}")
    if len(tasks) > limit:
        lines.append(f"... and {len(tasks) - limit} more")
    return "\n".join(lines)


def ok(summary: str, **kwargs: Any) -> ToolResult:
    return ToolResult.success(summary, **kwargs)


def fail(message: str, *, hint: str = "", error: str = "") -> ToolResult:
    return ToolResult.failure(message, hint=hint, error=error or message)


def bool_arg(arguments: dict[str, Any], name: str, default: bool = False) -> bool:
    value = arguments.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def int_arg(arguments: dict[str, Any], name: str, default: int) -> int:
    try:
        return int(arguments.get(name, default))
    except (TypeError, ValueError):
        return default


def string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


#: JSON schema fragments reused across tools.
SCHEMA_STRING = {"type": "string"}
SCHEMA_TEXT = {"type": "string", "maxLength": 20000}
SCHEMA_INT = {"type": "integer"}
