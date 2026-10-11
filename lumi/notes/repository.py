"""Notes: quick capture, search, pinning.

Notes are plain text in SQLite with an FTS5 index when available.  They are
*never* uploaded anywhere and survive restarts.  Nothing is deleted without an
explicit call - there is no background pruning of notes.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from lumi.memory.store import Database
from lumi.security.redact import redact_text
from lumi.utils.timeutil import from_iso, now, to_iso

log = logging.getLogger("lumi.notes")

MAX_TITLE = 200
MAX_BODY = 200_000


@dataclass
class Note:
    id: Optional[int] = None
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    source: str = "manual"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Any) -> "Note":
        tags_raw = row["tags"] or ""
        return cls(
            id=row["id"],
            title=row["title"] or "",
            body=row["body"] or "",
            tags=[tag.strip() for tag in tags_raw.split(",") if tag.strip()],
            pinned=bool(row["pinned"]),
            source=row["source"] or "manual",
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
        )

    def to_dict(self, *, body_limit: int = 4000) -> dict[str, Any]:
        body = self.body
        truncated = len(body) > body_limit
        return {
            "id": self.id,
            "title": self.title,
            "body": body[:body_limit],
            "body_truncated": truncated,
            "tags": list(self.tags),
            "pinned": self.pinned,
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
            "source": self.source,
        }

    def excerpt(self, *, length: int = 140) -> str:
        text = " ".join(self.body.split())
        return text[:length] + ("…" if len(text) > length else "")


class NoteRepository:
    """CRUD and search for notes."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------------- write
    def create(
        self,
        title: str,
        body: str = "",
        *,
        tags: Sequence[str] | None = None,
        pinned: bool = False,
        source: str = "manual",
    ) -> Note:
        clean_title = re.sub(r"\s+", " ", (title or "").strip())[:MAX_TITLE]
        clean_body = (body or "")[:MAX_BODY]
        if not clean_title and clean_body:
            clean_title = " ".join(clean_body.split())[:60]
        if not clean_title and not clean_body:
            raise ValueError("a note needs a title or some text")
        stamp = to_iso(now())
        note_id = self.db.execute_write(
            "INSERT INTO notes(title, body, tags, pinned, source, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (clean_title, clean_body, _join_tags(tags), int(pinned), source, stamp, stamp),
        )
        created = self.get(int(note_id))
        assert created is not None
        log.info("note created (id=%s, title=%r)", created.id, created.title[:60])
        return created

    def update(self, note_id: int, **changes: Any) -> Optional[Note]:
        fields: list[str] = []
        params: list[Any] = []
        if "title" in changes:
            fields.append("title = ?")
            params.append(re.sub(r"\s+", " ", str(changes["title"]).strip())[:MAX_TITLE])
        if "body" in changes:
            fields.append("body = ?")
            params.append(str(changes["body"])[:MAX_BODY])
        if "tags" in changes:
            fields.append("tags = ?")
            params.append(_join_tags(changes["tags"]))
        if "pinned" in changes:
            fields.append("pinned = ?")
            params.append(int(bool(changes["pinned"])))
        if not fields:
            return self.get(note_id)
        fields.append("updated_at = ?")
        params.append(to_iso(now()))
        params.append(int(note_id))
        self.db.execute_write(f"UPDATE notes SET {', '.join(fields)} WHERE id = ?", params)
        return self.get(int(note_id))

    def append(self, note_id: int, text: str, *, separator: str = "\n") -> Optional[Note]:
        note = self.get(note_id)
        if note is None:
            return None
        addition = (text or "").strip()
        if not addition:
            return note
        body = (note.body + separator + addition).strip() if note.body else addition
        return self.update(note_id, body=body)

    def toggle_pin(self, note_id: int) -> Optional[Note]:
        note = self.get(note_id)
        if note is None:
            return None
        return self.update(note_id, pinned=not note.pinned)

    def delete(self, note_id: int) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
            return cursor.rowcount > 0

    def clear(self) -> int:
        return int(self.db.execute_write("DELETE FROM notes") or 0)

    # ----------------------------------------------------------------- read
    def get(self, note_id: int) -> Optional[Note]:
        row = self.db.query_one("SELECT * FROM notes WHERE id = ?", (int(note_id),))
        return Note.from_row(row) if row else None

    def find_by_title(self, title: str, *, limit: int = 5) -> list[Note]:
        like = f"%{title.strip().lower()}%"
        rows = self.db.query(
            "SELECT * FROM notes WHERE lower(title) LIKE ? ORDER BY updated_at DESC LIMIT ?", (like, int(limit))
        )
        return [Note.from_row(row) for row in rows]

    def list(
        self,
        *,
        pinned_only: bool = False,
        tag: str | None = None,
        limit: int = 200,
        order: str = "pinned",
    ) -> list[Note]:
        sql = "SELECT * FROM notes WHERE 1=1"
        params: list[Any] = []
        if pinned_only:
            sql += " AND pinned = 1"
        if tag:
            sql += " AND (',' || lower(tags) || ',') LIKE ?"
            params.append(f"%,{tag.strip().lower()},%")
        orders = {
            "pinned": "pinned DESC, updated_at DESC, id DESC",
            "updated": "updated_at DESC, id DESC",
            "created": "created_at DESC, id DESC",
            "title": "lower(title) ASC",
        }
        sql += f" ORDER BY {orders.get(order, orders['pinned'])} LIMIT ?"
        params.append(int(limit))
        return [Note.from_row(row) for row in self.db.query(sql, params)]

    def search(self, query: str, *, limit: int = 25) -> list[Note]:
        text = " ".join((query or "").split())
        if not text:
            return []
        if self.db.fts_available():
            try:
                rows = self.db.query(
                    "SELECT n.* FROM notes_fts f JOIN notes n ON n.id = f.rowid WHERE notes_fts MATCH ? "
                    "ORDER BY bm25(notes_fts) LIMIT ?",
                    (_fts_query(text), int(limit)),
                )
                if rows:
                    return [Note.from_row(row) for row in rows]
            except sqlite3.OperationalError as exc:
                log.debug("FTS note search failed: %s", exc)
        like = f"%{text.lower()}%"
        rows = self.db.query(
            "SELECT * FROM notes WHERE lower(title) LIKE ? OR lower(body) LIKE ? OR lower(tags) LIKE ? "
            "ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (like, like, like, int(limit)),
        )
        return [Note.from_row(row) for row in rows]

    def tags(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for row in self.db.query("SELECT tags FROM notes"):
            for tag in (row["tags"] or "").split(","):
                cleaned = tag.strip().lower()
                if cleaned:
                    counts[cleaned] = counts.get(cleaned, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM notes", default=0) or 0)

    def pinned(self, *, limit: int = 10) -> list[Note]:
        return self.list(pinned_only=True, limit=limit)


def _join_tags(tags: Any) -> str:
    if isinstance(tags, str):
        items = [part.strip() for part in tags.split(",")]
    elif isinstance(tags, (list, tuple, set)):
        items = [str(part).strip() for part in tags]
    else:
        items = [str(tags).strip()] if tags else []
    return ",".join(tag for tag in items if tag)[:300]


def _fts_query(text: str) -> str:
    tokens = [token for token in re.split(r"[^\w]+", text) if token]
    return " ".join(f'"{token}"' for token in tokens[:12]) or '""'


def format_note_list(notes: Sequence[Note]) -> str:
    if not notes:
        return "No notes yet."
    lines: list[str] = []
    for note in notes:
        pin = "📌 " if note.pinned else ""
        tags = f" [{', '.join(note.tags)}]" if note.tags else ""
        lines.append(f"{pin}#{note.id} {note.title}{tags} - {redact_text(note.excerpt(), max_length=200)}")
    return "\n".join(lines)
