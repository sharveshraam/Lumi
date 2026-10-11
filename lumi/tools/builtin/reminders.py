"""Reminder tools.

Reminders are separate from tasks on purpose: a task can exist without a
deadline, and a reminder is a *notification time*.  A reminder always fires
through the local scheduler, so it survives a restart and reports clearly if it
was missed while the laptop was asleep.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable

from lumi.tasks import nlp
from lumi.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from lumi.tools.builtin._common import (
    SERVICE_REMINDERS,
    bool_arg,
    int_arg,
    ok,
    resolve_reminder,
    service,
)
from lumi.utils.timeutil import humanise_delta, humanise_due, now as current_time, to_iso

WHEN_REQUIRED = {
    "type": "string",
    "description": "When to remind, in natural language: 'in 20 minutes', 'tomorrow 6pm', 'daily 9am'.",
}


class SetReminder(Tool):
    name = "set_reminder"
    description = (
        "Schedule a reminder notification. Use repeat rules such as 'daily' or 'every Monday' for "
        "recurring reminders. The reminder fires from LUMI itself (a Windows notification)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The whole request, so the time can be parsed exactly."},
            "title": {"type": "string", "maxLength": 300, "description": "What to remind about (if you prefer to give the time separately)."},
            "when": WHEN_REQUIRED,
            "recurrence": {"type": "string", "description": "none, daily, weekdays, weekly, monthly, 'every 2 days'."},
            "body": {"type": "string", "maxLength": 1000, "description": "Extra detail shown with the notification."},
        },
    }
    category = "reminders"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0
    examples = ("remind me to drink water in 30 minutes", "remind me every Monday at 9am about the lab")

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        reminders = service(context, SERVICE_REMINDERS)
        text = str(arguments.get("text", "")).strip()
        title = str(arguments.get("title", "")).strip()
        when_text = str(arguments.get("when", "")).strip()
        recurrence = str(arguments.get("recurrence") or "none")

        parsed = None
        if text:
            parsed = nlp.parse_task_text(text, is_reminder=True)
            title = title or parsed.title
            when = parsed.remind_at or parsed.due_at
            if recurrence in {"", "none"} and parsed.recurrence not in {"", "none"}:
                recurrence = parsed.recurrence
            if when is None and when_text:
                when = nlp.parse_task_text(when_text, is_reminder=True).effective_when
        else:
            parsed_when = nlp.parse_task_text(when_text or "", is_reminder=True)
            when = parsed_when.effective_when

        if not title:
            raise ToolError(
                "I need to know what to remind you about.",
                hint="Try: 'remind me to submit the assignment tomorrow at 6 pm'.",
            )

        if when is None:
            when = current_time() + timedelta(minutes=15)
            note = "I could not read a time, so I set it for 15 minutes from now."
        else:
            note = ""

        if when <= current_time() and recurrence in {"", "none"}:
            when = current_time() + timedelta(minutes=1)
            note = (note + " That time had already passed, so I moved it to one minute from now.").strip()

        reminder = reminders.create(title, when, body=str(arguments.get("body", "")), recurrence=recurrence)

        scheduler = context.services.get("scheduler")
        if scheduler is not None:
            try:
                scheduler.wake()
            except Exception:  # pragma: no cover - best effort
                pass

        repeat_text = f", repeating {reminder.recurrence}" if reminder.repeats else ""
        message = f"Reminder set for {humanise_due(reminder.fire_at)}{repeat_text}: {reminder.title}. {note}".strip()
        return ok(
            message,
            data={"reminder": reminder.to_dict(), "assumptions": parsed.assumptions if parsed else []},
            verified=True,
        )


class ListReminders(Tool):
    name = "list_reminders"
    description = "List upcoming reminders, including any the user missed."
    parameters = {
        "type": "object",
        "properties": {
            "include_past": {"type": "boolean", "description": "Also show reminders that already fired."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    }
    category = "reminders"
    risk = RiskLevel.SAFE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        reminders = service(context, SERVICE_REMINDERS)
        limit = int_arg(arguments, "limit", 20)
        if bool_arg(arguments, "include_past", False):
            items = reminders.repo.list(limit=limit)
        else:
            items = reminders.repo.upcoming(limit=limit)
        if not items:
            return ok("No reminders are scheduled.", data={"reminders": []}, verified=True)
        lines = []
        for reminder in items:
            repeat = f" · repeats {reminder.recurrence}" if reminder.repeats else ""
            lines.append(f"#{reminder.id} {reminder.title} — {humanise_due(reminder.fire_at)}{repeat}")
        return ok(
            "\n".join(lines),
            data={"reminders": [reminder.to_dict() for reminder in items]},
            verified=True,
        )


class CancelReminder(Tool):
    name = "cancel_reminder"
    description = "Delete a scheduled reminder."
    parameters = {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "integer"},
            "title": {"type": "string", "description": "Part of the reminder text."},
        },
    }
    category = "reminders"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        reminders = service(context, SERVICE_REMINDERS)
        reminder = resolve_reminder(context, arguments)
        title, reminder_id = reminder.title, reminder.id
        if not reminders.cancel(int(reminder_id)):
            raise ToolError(f"Reminder #{reminder_id} was already gone.")
        return ok(f"Removed the reminder “{title}”.", data={"reminder_id": reminder_id}, verified=True)


class SnoozeReminder(Tool):
    name = "snooze_reminder"
    description = "Push a reminder back by a number of minutes (default 10)."
    parameters = {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "integer"},
            "title": {"type": "string"},
            "minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
        },
    }
    category = "reminders"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        reminders = service(context, SERVICE_REMINDERS)
        reminder = resolve_reminder(context, arguments)
        minutes = int_arg(arguments, "minutes", 10)
        updated = reminders.snooze(int(reminder.id), minutes=minutes)
        if updated is None:  # pragma: no cover
            raise ToolError(f"Reminder #{reminder.id} could not be snoozed.")
        return ok(
            f"Snoozed “{updated.title}” to {humanise_due(updated.fire_at)} ({humanise_delta(updated.fire_at)}).",
            data={"reminder": updated.to_dict()},
            verified=True,
        )


def build_tools() -> Iterable[Tool]:
    return (SetReminder(), ListReminders(), CancelReminder(), SnoozeReminder())
