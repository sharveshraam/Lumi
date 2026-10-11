"""File tools: search, read, write, organise.

Safety rules enforced here (and re-checked by the permission policy):

* reads are allowed in the user's folders, credentials are never readable,
* writes only inside the workspace folder, and only when the user allowed it,
* overwriting and deleting always show a preview and ask first,
* large or binary files are refused instead of dumped into the chat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from lumi.automation.files import (
    DEFAULT_ORGANISE_RULES,
    FileManager,
    describe_content_hits,
    describe_hits,
    human_size,
)
from lumi.security.policy import PermissionPolicy
from lumi.tools.base import ConfirmationRequest, RiskLevel, Tool, ToolContext, ToolError, ToolResult
from lumi.tools.builtin._common import (
    SERVICE_FILES,
    bool_arg,
    int_arg,
    ok,
    service,
    string_list,
)


def _manager(context: ToolContext) -> FileManager:
    try:
        return service(context, SERVICE_FILES)
    except ToolError:
        return FileManager(PermissionPolicy(context.settings))


class FindFiles(Tool):
    name = "find_files"
    description = (
        "Find files by name or extension inside the user's folders. Read-only. "
        "Use it before reading, moving or organising anything."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Part of the file name, or a pattern like 'lab*.pdf'."},
            "extension": {"type": "string", "description": "File type such as pdf, docx, py (the dot is optional)."},
            "folder": {"type": "string", "description": "Folder to search in. Defaults to the allowed folders."},
            "modified_within_days": {"type": "integer", "minimum": 1, "maximum": 3650},
            "min_size_kb": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    }
    category = "files"
    risk = RiskLevel.SAFE
    timeout_s = 30.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        roots = [str(arguments["folder"])] if arguments.get("folder") else None
        try:
            hits = manager.find_files(
                name_pattern=str(arguments.get("name") or "*"),
                extension=str(arguments.get("extension") or ""),
                roots=roots,
                max_results=int_arg(arguments, "limit", 40),
                modified_within_days=int(arguments["modified_within_days"]) if arguments.get("modified_within_days") else None,
                min_size_kb=int(arguments["min_size_kb"]) if arguments.get("min_size_kb") else None,
            )
        except PermissionError as exc:
            raise ToolError(str(exc))
        except ValueError as exc:
            raise ToolError(str(exc))
        text = describe_hits(hits)
        return ok(
            f"Found {len(hits)} file(s).\n\n{text}" if hits else "No files matched that search.",
            data={"files": [hit.to_dict() for hit in hits], "text": text},
            verified=True,
        )


class SearchInFiles(Tool):
    name = "search_in_files"
    description = (
        "Search *inside* supported text files (txt, md, code, csv, ...) for a word or phrase. "
        "Useful for finding a topic across your college notes or code."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Word or phrase to look for.", "minLength": 2, "maxLength": 120},
            "folder": {"type": "string"},
            "extensions": {"type": "array", "items": {"type": "string"}, "maxItems": 12, "description": "Limit to these file types, e.g. ['py','md']."},
            "case_sensitive": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["query"],
    }
    category = "files"
    risk = RiskLevel.SAFE
    timeout_s = 45.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        roots = [str(arguments["folder"])] if arguments.get("folder") else None
        try:
            hits = manager.search_in_files(
                str(arguments.get("query", "")),
                roots=roots,
                extensions=string_list(arguments.get("extensions")) or None,
                max_hits=int_arg(arguments, "limit", 40),
                case_sensitive=bool_arg(arguments, "case_sensitive", False),
            )
        except ValueError as exc:
            raise ToolError(str(exc))
        except PermissionError as exc:
            raise ToolError(str(exc))
        text = describe_content_hits(hits)
        return ok(
            f"{len(hits)} match(es).\n\n{text}" if hits else "Nothing matched inside those files.",
            data={"matches": [hit.to_dict() for hit in hits], "text": text},
            verified=True,
        )


class ReadTextFile(Tool):
    name = "read_text_file"
    description = (
        "Read a text-based file (txt, md, code, csv, json, ...) so you can explain, summarise or "
        "review it. Refuses files larger than 2 MB and never reads credential stores."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Full path to the file."},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 60000, "description": "How much to return (default 20000)."},
        },
        "required": ["path"],
    }
    category = "files"
    risk = RiskLevel.SAFE
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        path = Path(str(arguments.get("path", "")).strip().strip('"'))
        try:
            text = manager.read_text(path)
        except PermissionError as exc:
            raise ToolError(str(exc), hint="LUMI does not open credential files or folders outside your allowed areas.")
        except FileNotFoundError:
            raise ToolError(f"I could not find '{path}'.", hint="Search for the file first.")
        except ValueError as exc:
            raise ToolError(str(exc))
        except OSError as exc:
            raise ToolError(f"Could not read '{path}': {exc}")
        limit = int_arg(arguments, "max_chars", 20000)
        truncated = len(text) > limit
        payload = text[:limit]
        return ok(
            f"Read {len(text)} characters from {path.name}" + (" (truncated)." if truncated else ".") + f"\n\n{payload}",
            data={"path": str(path), "characters": len(text), "truncated": truncated, "text": payload},
            verified=True,
        )


class ReadDocument(Tool):
    name = "read_document"
    description = (
        "Extract the text of a document so it can be summarised or explained: .docx and .pdf "
        "(when the optional readers are installed) as well as plain text formats."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 60000},
        },
        "required": ["path"],
    }
    category = "files"
    risk = RiskLevel.SAFE
    timeout_s = 45.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        path = Path(str(arguments.get("path", "")).strip().strip('"'))
        try:
            summary = manager.summarize_document(path, max_chars=int_arg(arguments, "max_chars", 20000))
        except PermissionError as exc:
            raise ToolError(str(exc))
        except FileNotFoundError:
            raise ToolError(f"I could not find '{path}'.")
        except ValueError as exc:
            raise ToolError(
                str(exc),
                hint="For .docx/.pdf, install the optional readers: pip install python-docx pypdf",
            )
        except OSError as exc:
            raise ToolError(f"Could not read '{path}': {exc}")
        if not summary["text"].strip():
            raise ToolError(
                f"'{path.name}' did not contain any extractable text.",
                hint="Scanned PDFs need OCR, which LUMI does not do.",
            )
        return ok(
            f"Extracted {summary['words']} words from {path.name} ({summary['extension']}).\n\n{summary['text']}",
            data=summary,
            verified=True,
        )


class ListFolder(Tool):
    name = "list_folder"
    description = "List the contents of a folder with sizes and dates."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Folder to list. Defaults to the workspace folder."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
    }
    category = "files"
    risk = RiskLevel.SAFE
    timeout_s = 20.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        policy = PermissionPolicy(context.settings)
        raw = arguments.get("path")
        folder = Path(str(raw)).expanduser() if raw else (policy.allowed_roots()[0] if policy.allowed_roots() else Path.home())
        decision = policy.check_path(folder)
        if not decision.allowed:
            raise ToolError(decision.reason)
        if not folder.exists():
            raise ToolError(f"'{folder}' does not exist.")
        if not folder.is_dir():
            raise ToolError(f"'{folder}' is a file, not a folder.")
        limit = int_arg(arguments, "limit", 100)
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(folder.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as exc:
            raise ToolError(f"Could not read '{folder}': {exc}")
        for child in children[:limit]:
            try:
                stats = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_dir": child.is_dir(),
                    "size_human": "folder" if child.is_dir() else human_size(stats.st_size),
                    "size_bytes": 0 if child.is_dir() else stats.st_size,
                    "modified": stats.st_mtime,
                }
            )
        lines = [
            f"{'[dir] ' if entry['is_dir'] else '      '}{entry['name']} ({entry['size_human']})"
            for entry in entries
        ]
        return ok(
            f"{len(entries)} item(s) in {folder}:\n" + "\n".join(lines) if entries else f"{folder} is empty.",
            data={"folder": str(folder), "entries": entries},
            verified=True,
        )


class WriteTextFile(Tool):
    name = "write_text_file"
    description = (
        "Create a text file (or notes/document inside the workspace folder) and write content into it. "
        "If the file already exists, the user is asked before anything is replaced."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Full path of the file to write."},
            "content": {"type": "string", "maxLength": 200000, "description": "The text to write."},
            "overwrite": {"type": "boolean", "description": "Allow replacing an existing file (the user is still asked)."},
            "append": {"type": "boolean", "description": "Add to the end of the file instead of replacing it."},
        },
        "required": ["path", "content"],
    }
    category = "files"
    risk = RiskLevel.MODERATE
    timeout_s = 20.0
    permission_action = "write_file"

    def confirm_required(self, arguments: dict[str, Any], context: ToolContext) -> bool:
        path = Path(str(arguments.get("path", ""))).expanduser()
        if bool_arg(arguments, "append", False):
            return not path.exists()
        return path.exists()

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        path = Path(str(arguments.get("path", ""))).expanduser()
        content = str(arguments.get("content", ""))
        request = super().confirmation_details(arguments, context)
        request.title = "Replace this file?" if path.exists() else "Create this file?"
        request.details = f"{'Overwrite' if path.exists() else 'Create'} {path}"
        request.preview = (
            f"{len(content)} characters, {len(content.splitlines())} lines\n\n"
            + "\n".join(content.splitlines()[:15])
            + ("\n..." if len(content.splitlines()) > 15 else "")
        )
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        path = Path(str(arguments.get("path", "")).strip().strip('"'))
        content = str(arguments.get("content", ""))
        append = bool_arg(arguments, "append", False)
        overwrite = bool_arg(arguments, "overwrite", False)

        if append and path.exists():
            try:
                info = manager.append_text(path, content)
            except PermissionError as exc:
                raise ToolError(str(exc))
            except OSError as exc:
                raise ToolError(f"Could not write to '{path}': {exc}")
            return ok(
                f"Appended to {path.name} ({len(content)} characters).",
                data={"path": str(path), "bytes": info["bytes"]},
                verified=True,
            )

        try:
            info = manager.write_text(path, content, overwrite=overwrite)
        except PermissionError as exc:
            raise ToolError(
                str(exc),
                hint="Enable file writing in Settings → Permissions, or choose a path inside your workspace folder.",
            )
        except OSError as exc:
            raise ToolError(f"Could not write '{path}': {exc}")
        return ok(
            f"Wrote {info['bytes']} bytes to {path.name}.",
            data=info,
            verified=True,
        )


class PlanFileOrganisation(Tool):
    name = "plan_file_organisation"
    description = (
        "Work out how to tidy a folder (sort loose files into Documents, Images, Code, ...) and show "
        "the plan. Nothing moves until the user approves it, then call apply_file_organisation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "description": "Folder to tidy."},
            "apply": {"type": "boolean", "description": "Set true to carry out the plan after approval."},
        },
        "required": ["folder"],
    }
    category = "files"
    risk = RiskLevel.MODERATE
    timeout_s = 30.0
    permission_action = "bulk_file_operation"

    def confirm_required(self, arguments: dict[str, Any], context: ToolContext) -> bool:
        return bool_arg(arguments, "apply", False)

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        request = super().confirmation_details(arguments, context)
        manager = _manager(context)
        plan = manager.plan_organise(str(arguments.get("folder", "")))
        request.title = "Move these files?"
        request.details = f"Organise {arguments.get('folder')} ({len(plan.changes)} file(s))"
        request.preview = plan.preview()
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        plan = manager.plan_organise(str(arguments.get("folder", "")))
        if plan.blocked:
            raise ToolError(
                "That folder cannot be organised: " + plan.blocked[0],
                hint="Pick a folder inside your workspace, or allow it in Settings → Permissions.",
            )
        if plan.is_empty:
            return ok("That folder is already tidy - no files needed moving.", data=plan.to_dict(), verified=True)
        if not bool_arg(arguments, "apply", False):
            return ok(
                f"Here is what I would do ({len(plan.changes)} file(s)). Nothing has moved yet:\n\n{plan.preview()}",
                data=plan.to_dict(),
                verified=None,
            )
        apply_plan = manager.plan_move([str(change.source) for change in plan.changes], str(Path(str(arguments["folder"])) / "_lumi_stage"))
        results: list[dict[str, Any]] = []
        moved = 0
        for change in plan.changes:
            if change.source is None or change.target is None:
                continue
            change.target.parent.mkdir(parents=True, exist_ok=True)
            try:
                change.source.rename(change.target)
                moved += 1
                results.append({"source": str(change.source), "target": str(change.target), "ok": True})
            except OSError as exc:
                results.append({"source": str(change.source), "target": str(change.target), "ok": False, "error": str(exc)})
        del apply_plan  # the plan above is the authoritative preview
        return ok(
            f"Moved {moved} of {len(plan.changes)} file(s).",
            data={"moved": results, "plan": plan.to_dict()},
            verified=moved == len(plan.changes),
        )


class FindDuplicates(Tool):
    name = "find_duplicates"
    description = (
        "Find duplicate files in a folder by comparing size and content hashes. "
        "Reports them only - it never deletes anything."
    )
    parameters = {
        "type": "object",
        "properties": {
            "folder": {"type": "string"},
            "min_size_kb": {"type": "integer", "minimum": 1, "maximum": 1048576},
        },
        "required": ["folder"],
    }
    category = "files"
    risk = RiskLevel.SAFE
    timeout_s = 120.0

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        try:
            report = manager.find_duplicates(str(arguments.get("folder", "")), min_size_kb=int_arg(arguments, "min_size_kb", 4))
        except ValueError as exc:
            raise ToolError(str(exc))
        groups = report["groups"]
        if not groups:
            return ok("No duplicates found.", data=report, verified=True)
        lines = []
        for group in groups[:10]:
            lines.append(f"{group['count']} copies ({group['size_human']} each):")
            lines.extend(f"  - {path}" for path in group["files"][:6])
        return ok(
            f"{report['duplicate_files']} duplicate file(s) using about {report['wasted_human']} extra space.\n\n"
            + "\n".join(lines)
            + "\n\nNothing was deleted. Tell me which ones to remove and I will confirm first.",
            data=report,
            verified=True,
        )


class DeleteFiles(Tool):
    name = "delete_files"
    description = (
        "Delete files or folders, to the Recycle Bin where possible. Always shows exactly what will be "
        "removed and requires explicit confirmation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 200},
            "use_recycle_bin": {"type": "boolean", "description": "Default true (recoverable)."},
        },
        "required": ["paths"],
    }
    category = "files"
    risk = RiskLevel.CONSEQUENTIAL
    always_confirm = True
    bulk_capable = True
    confirmation_title = "Delete these files?"
    permission_action = "delete_file"
    timeout_s = 60.0

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        request = super().confirmation_details(arguments, context)
        manager = _manager(context)
        plan = manager.plan_delete(string_list(arguments.get("paths")))
        request.details = f"Delete {len(plan.changes)} item(s)"
        request.preview = plan.preview() + (
            "\n\nThey go to the Recycle Bin, so this is recoverable."
            if bool_arg(arguments, "use_recycle_bin", True)
            else "\n\nWARNING: this is a permanent delete."
        )
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        paths = string_list(arguments.get("paths"))
        if not paths:
            raise ToolError("No files were given to delete.")
        plan = manager.plan_delete(paths)
        if plan.is_empty:
            raise ToolError("None of those paths can be deleted: " + "; ".join(plan.blocked[:3]))
        results = manager.apply_delete(plan, use_recycle_bin=bool_arg(arguments, "use_recycle_bin", True))
        recycled = sum(1 for item in results if item.get("recycle_bin"))
        return ok(
            f"Deleted {len(results)} item(s)"
            + (f", {recycled} to the Recycle Bin." if recycled else " permanently."),
            data={"results": results, "blocked": plan.blocked},
            verified=True,
        )


class MoveFiles(Tool):
    name = "move_files"
    description = "Move or rename files. Shows the plan first and asks for confirmation."
    parameters = {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100},
            "destination": {"type": "string", "description": "Folder to move them into."},
            "rename_to": {"type": "string", "description": "New name (only valid with a single path)."},
        },
        "required": ["paths"],
    }
    category = "files"
    risk = RiskLevel.MODERATE
    timeout_s = 60.0
    bulk_capable = True
    permission_action = "move_file"

    def confirm_required(self, arguments: dict[str, Any], context: ToolContext) -> bool:
        return bool(arguments.get("destination") or arguments.get("rename_to"))

    def confirmation_details(self, arguments: dict[str, Any], context: ToolContext) -> ConfirmationRequest:
        manager = _manager(context)
        request = super().confirmation_details(arguments, context)
        paths = string_list(arguments.get("paths"))
        if arguments.get("rename_to"):
            plan = manager.plan_rename(paths[0], str(arguments["rename_to"])) if paths else None
        else:
            plan = manager.plan_move(paths, str(arguments.get("destination", "")))
        request.title = "Move these files?"
        request.details = f"{len(plan.changes) if plan else 0} item(s)"
        request.preview = plan.preview() if plan else "Nothing to do."
        return request

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = _manager(context)
        paths = string_list(arguments.get("paths"))
        if not paths:
            raise ToolError("No files were given.")
        if arguments.get("rename_to"):
            if len(paths) > 1:
                raise ToolError("Renaming works on one file at a time.")
            plan = manager.plan_rename(paths[0], str(arguments["rename_to"]))
            if plan.changes:
                results = manager.apply_rename(plan)
                return ok(
                    f"Renamed {Path(paths[0]).name} to {Path(results[0]['target']).name}.",
                    data={"results": results},
                    verified=True,
                )
        else:
            destination = str(arguments.get("destination", "")).strip()
            if not destination:
                raise ToolError("Tell me where to move them (destination folder).")
            plan = manager.plan_move(paths, destination)
            if plan.changes:
                results = manager.apply_move(plan)
                return ok(
                    f"Moved {len(results)} item(s) to {destination}.",
                    data={"results": results, "blocked": plan.blocked},
                    verified=len(results) == len(plan.changes),
                )
        raise ToolError("Nothing could be moved: " + "; ".join(plan.blocked[:3] or ["no valid sources"]))


def build_tools() -> Iterable[Tool]:
    return (
        FindFiles(),
        SearchInFiles(),
        ReadTextFile(),
        ReadDocument(),
        ListFolder(),
        WriteTextFile(),
        PlanFileOrganisation(),
        FindDuplicates(),
        DeleteFiles(),
        MoveFiles(),
    )
