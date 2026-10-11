"""Note tools: quick capture, search, edit, pin, delete."""

from __future__ import annotations

from typing import Any, Iterable

from lumi.notes.repository import format_note_list
from lumi.tools.base import ConfirmationRequest, RiskLevel, Tool, ToolContext, ToolError, ToolResult
from lumi.tools.builtin._common import (
    SERVICE_NOTES,
    bool_arg,
    int_arg,
    ok,
    resolve_note,
    service,
    string_list,
)


class CreateNote(Tool):
    name = "create_note"
    description = (
        "Save a note, snippet or quick thought locally. Use tags for subjects and pin it if it is "
        "something the user wants to keep seeing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200, "description": "Short title. Optional if the body is given."},
            "body": {"type": "string", "maxLength": 20000, "description": "The note text."},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "pinned": {"type": "boolean", "description": "Pin it so it stays at the top."},
        },
    }
    category = "notes"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        notes = service(context, SERVICE_NOTES)
        note = notes.create(
            str(arguments.get("title", "")),
            str(arguments.get("body", "")),
            tags=string_list(arguments.get("tags")),
            pinned=bool_arg(arguments, "pinned", False),
            source="chat",
        )
        return ok(
            f"Saved note #{note.id}: {note.title}"
            + (" (pinned)" if note.pinned else ""),
            data={"note": note.to_dict()},
            verified=True,
        )


class SearchNotes(Tool):
    name = "search_notes"
    description = "Search saved notes by words in the title, body or tags."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for."},
            "tag": {"type": "string", "description": "Only notes with this tag."},
            "pinned_only": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    }
    category = "notes"
    risk = RiskLevel.SAFE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        notes = service(context, SERVICE_NOTES)
        query = str(arguments.get("query", "")).strip()
        tag = str(arguments.get("tag", "")).strip()
        limit = int_arg(arguments, "limit", 20)
        if query:
            items = notes.search(query, limit=limit)
        else:
            items = notes.list(pinned_only=bool_arg(arguments, "pinned_only", False), tag=tag or None, limit=limit)
        if tag and query:
            items = [note for note in items if tag.lower() in {value.lower() for value in note.tags}]
        text = format_note_list(items)
        headline = f"Found {len(items)} note(s)." if query or tag else f"{len(items)} note(s)."
        return ok(
            headline + "\n\n" + text,
            data={"notes": [note.to_dict(body_limit=1200) for note in items], "text": text},
            verified=True,
        )


class ReadNote(Tool):
    name = "read_note"
    description = "Read the full text of one saved note."
    parameters = {
        "type": "object",
        "properties": {
            "note_id": {"type": "integer", "description": "Note number shown as #12."},
            "title": {"type": "string", "description": "Part of the note title."},
        },
    }
    category = "notes"
    risk = RiskLevel.SAFE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        note = resolve_note(context, arguments)
        return ok(
            f"#{note.id} {note.title}\n\n{note.body}",
            data={"note": note.to_dict(body_limit=20000)},
            verified=True,
        )


class UpdateNote(Tool):
    name = "update_note"
    description = "Edit a note: change its title or body, or append text to the end."
    parameters = {
        "type": "object",
        "properties": {
            "note_id": {"type": "integer"},
            "title": {"type": "string", "description": "Part of the note title, if the id is unknown."},
            "new_title": {"type": "string", "maxLength": 200},
            "body": {"type": "string", "maxLength": 20000, "description": "Replace the whole body with this."},
            "append": {"type": "string", "maxLength": 20000, "description": "Add this text at the end instead."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "pinned": {"type": "boolean"},
        },
    }
    category = "notes"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        notes = service(context, SERVICE_NOTES)
        note = resolve_note(context, arguments)
        changes: dict[str, Any] = {}
        if arguments.get("new_title"):
            changes["title"] = str(arguments["new_title"])
        if "body" in arguments and arguments["body"] is not None:
            changes["body"] = str(arguments["body"])
        if "tags" in arguments:
            changes["tags"] = string_list(arguments.get("tags"))
        if "pinned" in arguments:
            changes["pinned"] = bool_arg(arguments, "pinned")
        updated = note
        if changes:
            updated = notes.update(int(note.id), **changes) or note
        appended = False
        if arguments.get("append"):
            updated = notes.append(int(note.id), str(arguments["append"])) or updated
            appended = True
        if not changes and not appended:
            return ok(f"Nothing to change on #{note.id}.", data={"note": updated.to_dict()})
        return ok(
            f"Updated note #{updated.id} {updated.title}."
            + (" Text appended." if appended else ""),
            data={"note": updated.to_dict(body_limit=4000)},
            verified=True,
        )


class PinNote(Tool):
    name = "pin_note"
    description = "Pin or unpin a note so it appears at the top (and in the daily briefing)."
    parameters = {
        "type": "object",
        "properties": {
            "note_id": {"type": "integer"},
            "title": {"type": "string"},
            "pinned": {"type": "boolean", "description": "Defaults to pinning."},
        },
    }
    category = "notes"
    risk = RiskLevel.MODERATE
    timeout_s = 10.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        notes = service(context, SERVICE_NOTES)
        note = resolve_note(context, arguments)
        want = bool_arg(arguments, "pinned", not note.pinned)
        updated = notes.update(int(note.id), pinned=want) or note
        return ok(
            f"{'Pinned' if updated.pinned else 'Unpinned'} note #{updated.id} {updated.title}.",
            data={"note": updated.to_dict()},
            verified=True,
        )


class DeleteNote(Tool):
    name = "delete_note"
    description = "Delete a note permanently. Asks for confirmation before removing anything."
    parameters = {
        "type": "object",
        "properties": {"note_id": {"type": "integer"}, "title": {"type": "string"}},
    }
    category = "notes"
    risk = RiskLevel.CONSEQUENTIAL
    always_confirm = True
    confirmation_title = "Delete this note?"
    permission_action = "delete_note"
    timeout_s = 10.0

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        request = super().confirmation_details(arguments, context)
        try:
            note = resolve_note(context, arguments)
            request.details = f"Note #{note.id}: {note.title}"
            request.preview = note.excerpt(length=300) or "(empty note)"
        except ToolError:
            request.details = "Delete the requested note."
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        notes = service(context, SERVICE_NOTES)
        note = resolve_note(context, arguments)
        note_id, title = note.id, note.title
        if not notes.delete(int(note_id)):
            raise ToolError(f"Note #{note_id} was already gone.")
        return ok(f"Deleted note #{note_id} {title}.", data={"note_id": note_id}, verified=True)


def build_tools() -> Iterable[Tool]:
    return (CreateNote(), SearchNotes(), ReadNote(), UpdateNote(), PinNote(), DeleteNote())
