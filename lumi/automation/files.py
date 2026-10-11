"""Safe local file operations.

Everything here is built around three promises:

* **Read-only outside your folders.**  Searching may look in the folders you
  allowed; writing only happens inside your workspace folder.
* **Nothing destructive happens silently.**  Deleting and overwriting are
  prepared as a *plan* first, and the tool layer asks before applying it.
* **No surprises about content.**  Text files are size-capped and binary or
  credential-like files are refused, not dumped into a chat.

File operations never follow the "run a shell command" road: paths are resolved
and checked, and globs are built from sanitised patterns.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from lumi.security.policy import PermissionPolicy, sanitise_search_term
from lumi.utils.timeutil import from_iso, humanise_delta, now, to_iso

log = logging.getLogger("lumi.automation.files")

MAX_TEXT_BYTES = 2 * 1024 * 1024          # refuse to read more than 2 MB as text
MAX_SEARCH_RESULTS = 200
MAX_CONTENT_HITS = 40
SKIP_DIR_NAMES = frozenset(
    {
        "$recycle.bin", "system volume information", "node_modules", "__pycache__", ".git",
        ".venv", "venv", "env", "windows", "program files", "program files (x86)", "appdata",
        ".cache", ".npm", ".gradle", ".m2", "dist", "build", ".idea", ".vscode-test",
    }
)

TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
        ".ini", ".cfg", ".conf", ".toml", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".jsx",
        ".tsx", ".py", ".pyw", ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".kt", ".rs", ".go",
        ".sh", ".bat", ".ps1", ".sql", ".tex", ".v", ".sv", ".ino", ".m", ".r", ".ipynb",
    }
)

SEARCHABLE_EXTENSIONS = TEXT_EXTENSIONS | frozenset({".pdf", ".docx"})


@dataclass
class FileHit:
    path: Path
    size_bytes: int = 0
    modified_at: Optional[datetime] = None
    match_kind: str = "name"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "size_bytes": self.size_bytes,
            "size_human": human_size(self.size_bytes),
            "modified": to_iso(self.modified_at),
            "modified_human": humanise_delta(self.modified_at) if self.modified_at else "",
            "kind": self.match_kind,
        }


@dataclass
class ContentHit:
    path: Path
    line_number: int
    line: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "line": self.line_number, "text": self.line.strip()[:300]}


@dataclass
class PlannedChange:
    kind: str  # create | overwrite | move | copy | rename | delete | mkdir
    source: Optional[Path] = None
    target: Optional[Path] = None
    detail: str = ""
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": str(self.source) if self.source else "",
            "target": str(self.target) if self.target else "",
            "detail": self.detail,
            "destructive": self.destructive,
        }

    def human(self) -> str:
        if self.kind in {"move", "copy", "rename"}:
            return f"{self.kind}: {self.source} → {self.target}"
        if self.kind == "delete":
            return f"delete: {self.source}"
        if self.kind in {"create", "overwrite", "mkdir"}:
            return f"{self.kind}: {self.target or self.source}"
        return f"{self.kind}: {self.detail or self.source or self.target}"


@dataclass
class FilePlan:
    """A preview of what a bulk operation would do."""

    changes: list[PlannedChange] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def destructive(self) -> bool:
        return any(change.destructive for change in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": [change.to_dict() for change in self.changes],
            "blocked": list(self.blocked),
            "notes": list(self.notes),
            "count": len(self.changes),
            "destructive": self.destructive,
        }

    def preview(self, *, limit: int = 25) -> str:
        lines = [change.human() for change in self.changes[:limit]]
        if len(self.changes) > limit:
            lines.append(f"... and {len(self.changes) - limit} more")
        if self.blocked:
            lines.append("Skipped (not allowed):")
            lines.extend(f"  - {item}" for item in self.blocked[:8])
        return "\n".join(lines) if lines else "Nothing to do."


def human_size(num_bytes: int) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


class FileManager:
    """Read/search/write files, always through a :class:`PermissionPolicy`."""

    def __init__(self, policy: PermissionPolicy) -> None:
        self.policy = policy

    # ------------------------------------------------------------------ search
    def find_files(
        self,
        *,
        name_pattern: str = "*",
        extension: str = "",
        roots: Sequence[str] | None = None,
        max_results: int = 50,
        max_depth: int = 6,
        modified_within_days: int | None = None,
        min_size_kb: int | None = None,
        include_hidden: bool = False,
    ) -> list[FileHit]:
        pattern = sanitise_search_term(name_pattern) or "*"
        if extension and not extension.startswith("."):
            extension = "." + extension
        if not any(char in pattern for char in "*?"):
            pattern = f"*{pattern}*"
        search_roots = self._resolve_roots(roots)
        if not search_roots:
            raise ValueError("No folders are available to search. Add one in Settings → Permissions.")

        cutoff = None
        if modified_within_days:
            from datetime import timedelta

            cutoff = now() - timedelta(days=int(modified_within_days))

        hits: list[FileHit] = []
        for root in search_roots:
            if not root.exists():
                continue
            for path in _walk(root, max_depth=max_depth, include_hidden=include_hidden):
                if len(hits) >= max_results:
                    return hits
                if not path.is_file():
                    continue
                if extension and path.suffix.lower() != extension.lower():
                    continue
                if not fnmatch.fnmatch(path.name.lower(), pattern.lower()):
                    continue
                allowed = self.policy.check_path(path, write=False)
                if not allowed.allowed:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if cutoff is not None and datetime.fromtimestamp(stat.st_mtime) < cutoff:
                    continue
                if min_size_kb is not None and stat.st_size < int(min_size_kb) * 1024:
                    continue
                hits.append(
                    FileHit(path=path, size_bytes=stat.st_size, modified_at=datetime.fromtimestamp(stat.st_mtime))
                )
        hits.sort(key=lambda hit: (hit.modified_at is None, -(hit.modified_at.timestamp() if hit.modified_at else 0)))
        return hits[:max_results]

    def search_in_files(
        self,
        needle: str,
        *,
        roots: Sequence[str] | None = None,
        extensions: Sequence[str] | None = None,
        max_files: int = 400,
        max_hits: int = MAX_CONTENT_HITS,
        case_sensitive: bool = False,
        include_hidden: bool = False,
    ) -> list[ContentHit]:
        term = (needle or "").strip()
        if len(term) < 2:
            raise ValueError("Give me at least two characters to search for.")
        if len(term) > 120:
            raise ValueError("That search text is too long.")
        search_roots = self._resolve_roots(roots)
        allowed_extensions = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in (extensions or [])}
        hits: list[ContentHit] = []
        candidates: list[Path] = []
        for root in search_roots:
            if not root.exists():
                continue
            for path in _walk(root, max_depth=7, include_hidden=include_hidden):
                if len(candidates) >= max_files:
                    break
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if allowed_extensions and suffix not in allowed_extensions:
                    continue
                if not allowed_extensions and suffix not in TEXT_EXTENSIONS:
                    continue
                if not self.policy.check_path(path).allowed:
                    continue
                candidates.append(path)

        for path in candidates:
            if len(hits) >= max_hits:
                break
            try:
                if path.stat().st_size > MAX_TEXT_BYTES:
                    continue
                text = read_text(path, max_bytes=MAX_TEXT_BYTES)
            except (OSError, UnicodeError, ValueError):
                continue
            haystack = text if case_sensitive else text.lower()
            target = term if case_sensitive else term.lower()
            if target not in haystack:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                comparison = line if case_sensitive else line.lower()
                if target in comparison:
                    hits.append(ContentHit(path=path, line_number=number, line=line[:300]))
                    if len(hits) >= max_hits:
                        break
        return hits

    def read_text(self, path: str | os.PathLike[str], *, max_bytes: int = MAX_TEXT_BYTES) -> str:
        resolved = Path(path).expanduser()
        decision = self.policy.check_path(resolved)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        if resolved.is_dir():
            raise IsADirectoryError(f"'{resolved}' is a folder, not a file.")
        return read_text(resolved, max_bytes=max_bytes)

    def summarize_document(self, path: str | os.PathLike[str], *, max_chars: int = 6000) -> dict[str, Any]:
        """Extract readable text from a supported document, without AI."""
        resolved = Path(path).expanduser()
        decision = self.policy.check_path(resolved)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        suffix = resolved.suffix.lower()
        if suffix in {".docx"}:
            text = read_docx(resolved) or ""
        elif suffix == ".pdf":
            text = read_pdf(resolved) or ""
        elif suffix in TEXT_EXTENSIONS or suffix == "":
            text = read_text(resolved)
        else:
            raise ValueError(
                f"'{suffix or 'this file type'}' is not a format LUMI can read as text. "
                "Supported: plain text, Markdown, code files, .docx and .pdf."
            )
        stats = resolved.stat()
        return {
            "path": str(resolved),
            "characters": len(text),
            "words": len(text.split()),
            "modified": to_iso(datetime.fromtimestamp(stats.st_mtime)),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "extension": suffix,
        }

    # ------------------------------------------------------------- writing
    def plan_write(self, path: str | os.PathLike[str], *, overwrite: bool = False) -> FilePlan:
        """Check whether writing is allowed, and whether it would overwrite."""
        target = Path(path).expanduser()
        plan = FilePlan()
        parent = target.parent
        if not parent.exists():
            plan.changes.append(PlannedChange(kind="mkdir", target=parent, detail="folder will be created"))
        allowed = self.policy.check_path(target, write=True)
        if not allowed.allowed:
            plan.blocked.append(allowed.reason)
            return plan
        if target.exists():
            if target.is_dir():
                plan.blocked.append(f"'{target}' is a folder.")
                return plan
            if overwrite:
                stats = target.stat()
                plan.changes.append(
                    PlannedChange(
                        kind="overwrite",
                        target=target,
                        detail=f"replaces an existing file ({human_size(stats.st_size)}, "
                        f"{to_iso(datetime.fromtimestamp(stats.st_mtime))})",
                        destructive=True,
                    )
                )
            else:
                plan.blocked.append(f"'{target.name}' already exists (I will not overwrite it without permission).")
            return plan
        # Detect a similarly named file so the user is not fooled by a sneaky edit.
        plan.changes.append(PlannedChange(kind="create", target=target, detail="new file"))
        return plan

    def write_text(self, path: str | os.PathLike[str], content: str, *, overwrite: bool = False) -> dict[str, Any]:
        """Write a text file atomically.  Caller must have checked :meth:`plan_write`."""
        target = Path(path).expanduser()
        plan = self.plan_write(target, overwrite=overwrite)
        if plan.blocked:
            raise PermissionError(plan.blocked[0])
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = content if isinstance(content, str) else str(content)
        temp = target.with_suffix(target.suffix + ".lumi-tmp")
        temp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temp, target)
        stats = target.stat()
        log.info("wrote %s bytes to %s", stats.st_size, target)
        return {
            "path": str(target),
            "bytes": stats.st_size,
            "created_folders": [str(change.target) for change in plan.changes if change.kind == "mkdir"],
            "overwrote": bool(target.exists() and overwrite),
        }

    def append_text(self, path: str | os.PathLike[str], content: str) -> dict[str, Any]:
        target = Path(path).expanduser()
        allowed = self.policy.check_path(target, write=True)
        if not allowed.allowed:
            raise PermissionError(allowed.reason)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(content if content.endswith("\n") else content + "\n")
        return {"path": str(target), "bytes": target.stat().st_size}

    # ---------------------------------------------------- bulk operations
    def plan_move(self, sources: Iterable[str | os.PathLike[str]], target_dir: str | os.PathLike[str]) -> FilePlan:
        plan = FilePlan()
        destination = Path(target_dir).expanduser()
        allowed_dir = self.policy.check_path(destination, write=True)
        if not allowed_dir.allowed:
            plan.blocked.append(allowed_dir.reason)
            return plan
        if destination.exists() and not destination.is_dir():
            plan.blocked.append(f"'{destination}' exists and is not a folder.")
            return plan
        for source in sources:
            path = Path(source).expanduser()
            if not path.exists():
                plan.blocked.append(f"'{path}' does not exist.")
                continue
            allowed = self.policy.check_path(path, write=True)
            if not allowed.allowed:
                plan.blocked.append(f"{path.name}: {allowed.reason}")
                continue
            target = destination / path.name
            if target.exists():
                plan.blocked.append(f"{path.name}: a file with that name is already in the destination.")
                continue
            plan.changes.append(PlannedChange(kind="move", source=path, target=target, detail="move"))
        return plan

    def apply_move(self, plan: FilePlan) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for change in plan.changes:
            if change.kind != "move" or change.source is None or change.target is None:
                continue
            change.target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(change.source), str(change.target))
            results.append({"source": str(change.source), "target": str(change.target), "ok": True})
        return results

    def plan_rename(self, path: str | os.PathLike[str], new_name: str) -> FilePlan:
        plan = FilePlan()
        source = Path(path).expanduser()
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", new_name).strip().strip(".")
        if not safe_name:
            plan.blocked.append("That file name is not valid.")
            return plan
        target = source.with_name(safe_name)
        allowed = self.policy.check_path(source, write=True)
        if not allowed.allowed:
            plan.blocked.append(allowed.reason)
            return plan
        if target.exists():
            plan.blocked.append(f"'{target.name}' already exists.")
            return plan
        plan.changes.append(PlannedChange(kind="rename", source=source, target=target, detail="rename"))
        return plan

    def apply_rename(self, plan: FilePlan) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for change in plan.changes:
            if change.kind != "rename" or change.source is None or change.target is None:
                continue
            change.source.rename(change.target)
            results.append({"source": str(change.source), "target": str(change.target), "ok": True})
        return results

    def plan_delete(self, paths: Iterable[str | os.PathLike[str]]) -> FilePlan:
        """Deleting always requires confirmation; it is never silent."""
        plan = FilePlan()
        for item in paths:
            path = Path(item).expanduser()
            if not path.exists():
                plan.blocked.append(f"'{path}' does not exist.")
                continue
            allowed = self.policy.check_path(path, write=True)
            if not allowed.allowed:
                plan.blocked.append(f"{path}: {allowed.reason}")
                continue
            if path.is_dir():
                count = sum(1 for _ in path.rglob("*"))
                plan.changes.append(
                    PlannedChange(kind="delete", source=path, detail=f"folder with {count} item(s)", destructive=True)
                )
            else:
                plan.changes.append(
                    PlannedChange(kind="delete", source=path, detail=f"file ({human_size(path.stat().st_size)})", destructive=True)
                )
        if len(plan.changes) > 1:
            plan.notes.append("Bulk delete - everything listed will be removed together.")
        return plan

    def apply_delete(self, plan: FilePlan, *, use_recycle_bin: bool = True) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for change in plan.changes:
            if change.kind != "delete" or change.source is None:
                continue
            path = change.source
            moved_to_bin = False
            if use_recycle_bin:
                moved_to_bin = _send_to_recycle_bin(path)
            if not moved_to_bin:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            results.append({"path": str(path), "ok": True, "recycle_bin": moved_to_bin})
        return results

    def plan_organise(self, folder: str | os.PathLike[str], *, rules: dict[str, Sequence[str]] | None = None) -> FilePlan:
        """Group loose files into category folders - preview only until approved."""
        root = Path(folder).expanduser()
        plan = FilePlan()
        if not root.exists() or not root.is_dir():
            plan.blocked.append(f"'{root}' is not a folder.")
            return plan
        allowed = self.policy.check_path(root, write=True)
        if not allowed.allowed:
            plan.blocked.append(allowed.reason)
            return plan
        mapping = {key.lower(): tuple(value) for key, value in (rules or DEFAULT_ORGANISE_RULES).items()}
        for item in sorted(root.iterdir()):
            if item.is_dir() or item.name.startswith("."):
                continue
            if not self.policy.check_path(item, write=True).allowed:
                plan.blocked.append(f"{item.name}: not allowed")
                continue
            category = _categorise(item, mapping)
            if category is None:
                continue
            destination = root / category / item.name
            if destination.exists():
                plan.blocked.append(f"{item.name}: '{category}/{item.name}' already exists")
                continue
            plan.changes.append(PlannedChange(kind="move", source=item, target=destination, detail=f"into {category}/"))
        if plan.changes:
            plan.notes.append("Nothing moves until you approve this preview.")
        return plan

    def find_duplicates(
        self,
        folder: str | os.PathLike[str],
        *,
        min_size_kb: int = 4,
        max_files: int = 4000,
    ) -> dict[str, Any]:
        """Group identical files by size then hash.  Never deletes anything."""
        root = Path(folder).expanduser()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"'{root}' is not a folder.")
        by_size: dict[int, list[Path]] = {}
        considered = 0
        for path in _walk(root, max_depth=6):
            if considered >= max_files:
                break
            if not path.is_file():
                continue
            if not self.policy.check_path(path).allowed:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < min_size_kb * 1024:
                continue
            considered += 1
            by_size.setdefault(size, []).append(path)

        groups: list[dict[str, Any]] = []
        wasted = 0
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            by_hash: dict[str, list[Path]] = {}
            for path in paths:
                digest = _hash_file(path)
                if digest is None:
                    continue
                by_hash.setdefault(digest, []).append(path)
            for digest, matches in by_hash.items():
                if len(matches) < 2:
                    continue
                wasted += size * (len(matches) - 1)
                groups.append(
                    {
                        "hash": digest[:12],
                        "size_bytes": size,
                        "size_human": human_size(size),
                        "count": len(matches),
                        "files": [str(path) for path in matches],
                    }
                )
        groups.sort(key=lambda group: -(group["size_bytes"] * (group["count"] - 1)))
        return {
            "folder": str(root),
            "groups": groups,
            "duplicate_files": sum(group["count"] - 1 for group in groups),
            "wasted_bytes": wasted,
            "wasted_human": human_size(wasted),
            "note": "Nothing was deleted. Review the list and tell me what to remove.",
        }

    # ---------------------------------------------------------------- helpers
    def _resolve_roots(self, roots: Sequence[str] | None) -> list[Path]:
        if roots:
            candidates = [Path(item).expanduser() for item in roots]
        else:
            candidates = list(self.policy.search_roots())
        resolved: list[Path] = []
        for candidate in candidates:
            if self.policy.check_path(candidate).allowed:
                resolved.append(candidate)
        return resolved


DEFAULT_ORGANISE_RULES: dict[str, Sequence[str]] = {
    "Documents": (".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md"),
    "Spreadsheets": (".xls", ".xlsx", ".csv", ".ods", ".tsv"),
    "Slides": (".ppt", ".pptx", ".odp"),
    "Images": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic"),
    "Audio": (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"),
    "Video": (".mp4", ".mkv", ".mov", ".avi", ".webm"),
    "Archives": (".zip", ".rar", ".7z", ".tar", ".gz"),
    "Code": (".py", ".c", ".h", ".cpp", ".java", ".js", ".ts", ".ino", ".v", ".sv", ".ipynb"),
    "Installers": (".exe", ".msi", ".dmg", ".deb", ".apk"),
}


def _categorise(path: Path, mapping: dict[str, tuple[str, ...]]) -> Optional[str]:
    suffix = path.suffix.lower()
    for category, extensions in mapping.items():
        if suffix in extensions:
            return category.capitalize() if category.islower() else category
    return None


def _walk(root: Path, *, max_depth: int, include_hidden: bool = False) -> Iterable[Path]:
    """Depth-limited walk that skips noisy/system folders."""
    root = Path(root)
    if root.is_file():
        yield root
        return
    base_depth = len(root.parts)
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [
                name
                for name in dirs
                if name.lower() not in SKIP_DIR_NAMES and (include_hidden or not name.startswith("."))
            ]
        for name in files:
            if not include_hidden and name.startswith("."):
                continue
            yield current_path / name


def read_text(path: Path, *, max_bytes: int = MAX_TEXT_BYTES) -> str:
    """Read a text file with encoding detection and a hard size cap."""
    stats = path.stat()
    if stats.st_size > max_bytes:
        raise ValueError(
            f"'{path.name}' is {human_size(stats.st_size)}, which is too large to read as text "
            f"(limit {human_size(max_bytes)})."
        )
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def read_docx(path: Path) -> Optional[str]:
    """Read .docx text using python-docx when available, else the raw XML."""
    try:  # pragma: no cover - optional dependency
        import docx  # type: ignore

        document = docx.Document(str(path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception:
        pass
    try:
        import zipfile
        from xml.etree import ElementTree

        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        lines = []
        for paragraph in root.iter(f"{namespace}p"):
            text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
            if text.strip():
                lines.append(text)
        return "\n".join(lines)
    except Exception as exc:
        log.debug("could not read docx %s: %s", path, exc)
        return None


def read_pdf(path: Path) -> Optional[str]:
    """Read .pdf text using pypdf when installed (optional dependency)."""
    try:  # pragma: no cover - optional dependency
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages[:200]:
            pages.append(page.extract_text() or "")
        return "\n".join(pages)
    except Exception as exc:
        log.debug("could not read pdf %s: %s", path, exc)
        return None


def _hash_file(path: Path, *, chunk: int = 1 << 20) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _send_to_recycle_bin(path: Path) -> bool:
    """Move to the Recycle Bin / Trash when the platform supports it."""
    try:  # pragma: no cover - optional dependency
        from send2trash import send2trash  # type: ignore

        send2trash(str(path))
        return True
    except Exception:
        return False


def describe_hits(hits: Sequence[FileHit], *, limit: int = 20) -> str:
    if not hits:
        return "No matching files."
    lines = [
        f"- {hit.path}  ({human_size(hit.size_bytes)}, modified {humanise_delta(hit.modified_at) if hit.modified_at else 'unknown'})"
        for hit in hits[:limit]
    ]
    if len(hits) > limit:
        lines.append(f"... and {len(hits) - limit} more")
    return "\n".join(lines)


def describe_content_hits(hits: Sequence[ContentHit], *, limit: int = 20) -> str:
    if not hits:
        return "Nothing matched inside those files."
    lines = [f"- {hit.path}:{hit.line_number}: {hit.line.strip()[:160]}" for hit in hits[:limit]]
    if len(hits) > limit:
        lines.append(f"... and {len(hits) - limit} more matches")
    return "\n".join(lines)
