"""Persistent memory and conversation history.

Design decisions the user can rely on:

* **Nothing is remembered implicitly.**  ``remember_from_chat`` defaults to
  ``False``; the chat layer asks first (see :mod:`lumi.ai.assistant`).
* **Session context is RAM only.**  :data:`KIND_SESSION` entries live in a
  dictionary for the lifetime of the process and are never written to SQLite.
* **Sensitive content is refused.**  :func:`looks_sensitive` blocks passwords,
  tokens, card-like digit runs and ID numbers from being stored.
* **Everything is inspectable.**  Export/import is plain JSON, and the memory
  viewer in the UI edits the same rows these methods expose.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from lumi.memory.models import (
    KIND_FACT,
    KIND_PREFERENCE,
    KIND_PROJECT,
    KIND_SESSION,
    MEMORY_KINDS,
    Conversation,
    Memory,
    Message,
)
from lumi.memory.store import Database
from lumi.security.redact import redact_text
from lumi.utils.timeutil import add_months, now, to_iso

log = logging.getLogger("lumi.memory")

MAX_MEMORY_CHARS = 2000
MAX_MEMORIES_FOR_PROMPT = 8

_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "password: x", "password = x", "my password is x"
    re.compile(r"(?i)\b(password|passwd|pwd)\b\s*(?::|=|\bis\b)"),
    re.compile(r"(?i)\b(api[\s_-]?key|secret|token|bearer)\b\s*[:=]"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{12,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
    re.compile(r"\b\d{13,19}\b"),          # card / account-like digit runs
    re.compile(r"(?i)\b(cvv|cvc)\b\s*[:=]?\s*\d{3,4}\b"),
    re.compile(r"(?i)\b(aadhaar|aadhar|ssn|pan)\b\s*(number|no\.?)?\s*[:=]?\s*[A-Z0-9]{6,}"),
    re.compile(r"(?i)\b(otp|one[\s-]?time[\s-]?code)\b\s*[:=]?\s*\d{4,8}\b"),
    re.compile(r"(?i)\b(pin|passcode)\b\s*(?:code|number|no\.?)?\s*[:=]?\s*\d{4,8}\b"),
)


def looks_sensitive(text: str) -> Optional[str]:
    """Return a human explanation when ``text`` looks like a secret to keep out of memory."""
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return (
                "That looks like a credential or an identity number. LUMI does not store "
                "passwords, tokens, card numbers or ID numbers in memory."
            )
    return None


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


class MemoryService:
    """CRUD + search for long-term memory, plus RAM-only session context."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._session: dict[str, Memory] = {}

    # -------------------------------------------------------------- session
    def set_session_context(self, key: str, content: str) -> Memory:
        entry = Memory(kind=KIND_SESSION, content=normalise(content), key=normalise(key), source="session", created_at=now(), updated_at=now())
        self._session[normalise(key).lower()] = entry
        return entry

    def session_context(self) -> list[Memory]:
        return list(self._session.values())

    def clear_session_context(self) -> None:
        self._session.clear()

    # ------------------------------------------------------------------ add
    def add(
        self,
        content: str,
        *,
        kind: str = KIND_FACT,
        key: str = "",
        source: str = "manual",
        confidence: float = 1.0,
        pinned: bool = False,
        allow_sensitive: bool = False,
    ) -> Memory:
        """Store a memory.  Raises :class:`ValueError` for empty/sensitive input."""
        text = normalise(content)
        if not text:
            raise ValueError("memory content cannot be empty")
        if len(text) > MAX_MEMORY_CHARS:
            raise ValueError(f"memory content is too long (limit {MAX_MEMORY_CHARS} characters)")
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {kind!r}")
        if kind == KIND_SESSION:
            return self.set_session_context(key or text[:40], text)
        if not allow_sensitive:
            warning = looks_sensitive(text)
            if warning:
                raise ValueError(warning)

        stamp = to_iso(now())
        memory_id = self.db.execute_write(
            """
            INSERT INTO memories(kind, content, key, source, confidence, pinned, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, text, key or None, source, max(0.0, min(1.0, confidence)), int(pinned), stamp, stamp),
        )
        log.info("memory added (kind=%s, id=%s)", kind, memory_id)
        created = self.get(int(memory_id))
        assert created is not None
        return created

    def upsert_by_key(self, key: str, content: str, *, kind: str = KIND_PREFERENCE, **kwargs: Any) -> Memory:
        """Store or replace the memory with this key (used for stable preferences)."""
        existing = self.find_by_key(key)
        if existing and existing.id is not None:
            return self.update(existing.id, content=content, kind=kind) or existing
        return self.add(content, kind=kind, key=key, **kwargs)

    # ---------------------------------------------------------------- read
    def get(self, memory_id: int) -> Optional[Memory]:
        row = self.db.query_one("SELECT * FROM memories WHERE id = ?", (int(memory_id),))
        return Memory.from_row(row) if row else None

    def find_by_key(self, key: str) -> Optional[Memory]:
        row = self.db.query_one(
            "SELECT * FROM memories WHERE lower(coalesce(key, '')) = lower(?) ORDER BY id LIMIT 1",
            (normalise(key),),
        )
        return Memory.from_row(row) if row else None

    def list(
        self,
        *,
        kind: Optional[str] = None,
        limit: int = 200,
        order: str = "updated_at DESC, id DESC",
    ) -> list[Memory]:
        safe_order = _safe_order(order)
        if kind:
            rows = self.db.query(
                f"SELECT * FROM memories WHERE kind = ? ORDER BY {safe_order} LIMIT ?", (kind, int(limit))
            )
        else:
            rows = self.db.query(f"SELECT * FROM memories ORDER BY {safe_order} LIMIT ?", (int(limit),))
        return [Memory.from_row(row) for row in rows] + self._matching_session(kind, limit)

    def _matching_session(self, kind: Optional[str], limit: int) -> list[Memory]:
        if kind and kind != KIND_SESSION:
            return []
        return list(self._session.values())[:limit]

    def search(self, query: str, *, kinds: Optional[Sequence[str]] = None, limit: int = 25) -> list[Memory]:
        """Search memories.  Uses FTS5 when available, otherwise a LIKE scan."""
        text = normalise(query)
        if not text:
            return self.list(limit=limit)
        results: list[Memory] = []
        kind_filter = [k for k in (kinds or []) if k in MEMORY_KINDS and k != KIND_SESSION]

        if self.db.fts_available():
            try:
                sql = (
                    "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? "
                )
                params: list[Any] = [_fts_query(text)]
                if kind_filter:
                    sql += f"AND m.kind IN ({','.join('?' * len(kind_filter))}) "
                    params.extend(kind_filter)
                sql += "ORDER BY bm25(memories_fts) LIMIT ?"
                params.append(int(limit))
                results = [Memory.from_row(row) for row in self.db.query(sql, params)]
            except sqlite3.OperationalError as exc:
                log.debug("FTS memory search failed (%s); falling back to LIKE", exc)

        if not results:
            like = f"%{text.lower()}%"
            sql = "SELECT * FROM memories WHERE (lower(content) LIKE ? OR lower(coalesce(key,'')) LIKE ?) "
            params = [like, like]
            if kind_filter:
                sql += f"AND kind IN ({','.join('?' * len(kind_filter))}) "
                params.extend(kind_filter)
            sql += "ORDER BY updated_at DESC LIMIT ?"
            params.append(int(limit))
            results = [Memory.from_row(row) for row in self.db.query(sql, params)]

        if not kinds or KIND_SESSION in kinds:
            lowered = text.lower()
            for entry in self._session.values():
                if lowered in entry.content.lower() or lowered in entry.key.lower():
                    results.append(entry)
        return results[:limit]

    def relevant_for_prompt(self, query: str = "", *, limit: int = MAX_MEMORIES_FOR_PROMPT) -> list[Memory]:
        """Pick the memories worth putting in the system prompt.

        Pinned memories always come first, then key matches, then recency.  This
        keeps the prompt small on modest hardware.
        """
        chosen: list[Memory] = []
        seen: set[int] = set()

        def push(entry: Memory) -> None:
            marker = entry.id if entry.id is not None else -hash(entry.content)
            if marker in seen:
                return
            seen.add(marker)
            chosen.append(entry)

        for entry in self.list(kind=KIND_PREFERENCE, limit=limit):
            if entry.pinned:
                push(entry)
        if query.strip():
            for entry in self.search(query, limit=limit):
                push(entry)
                if len(chosen) >= limit:
                    break
        for entry in self.list(limit=limit * 2):
            if len(chosen) >= limit:
                break
            push(entry)
        for entry in self.session_context():
            if len(chosen) >= limit + 2:
                break
            push(entry)
        return chosen[: limit + 2]

    # -------------------------------------------------------------- update
    def update(self, memory_id: int, **changes: Any) -> Optional[Memory]:
        if memory_id is None:
            raise ValueError("memory id required")
        fields: list[str] = []
        params: list[Any] = []
        if "content" in changes:
            content = normalise(str(changes["content"]))
            if not content:
                raise ValueError("memory content cannot be empty")
            if len(content) > MAX_MEMORY_CHARS:
                raise ValueError("memory content is too long")
            warning = looks_sensitive(content)
            if warning and not changes.get("allow_sensitive"):
                raise ValueError(warning)
            fields.append("content = ?")
            params.append(content)
        if "kind" in changes:
            kind = str(changes["kind"])
            if kind not in MEMORY_KINDS or kind == KIND_SESSION:
                raise ValueError(f"cannot set kind to {kind!r}")
            fields.append("kind = ?")
            params.append(kind)
        if "key" in changes:
            fields.append("key = ?")
            params.append(normalise(str(changes["key"])) or None)
        if "pinned" in changes:
            fields.append("pinned = ?")
            params.append(int(bool(changes["pinned"])))
        if "confidence" in changes:
            fields.append("confidence = ?")
            params.append(max(0.0, min(1.0, float(changes["confidence"]))))

        if not fields:
            return self.get(memory_id)

        fields.append("updated_at = ?")
        params.append(to_iso(now()))
        params.append(int(memory_id))
        self.db.execute_write(f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", params)
        return self.get(int(memory_id))

    def touch(self, memory_id: int) -> None:
        self.db.execute_write(
            "UPDATE memories SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
            (to_iso(now()), int(memory_id)),
        )

    def delete(self, memory_id: int) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            return cursor.rowcount > 0

    def clear(self, *, kinds: Optional[Sequence[str]] = None) -> int:
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            count = self.db.execute_write(f"DELETE FROM memories WHERE kind IN ({placeholders})", list(kinds))
        else:
            count = self.db.execute_write("DELETE FROM memories")
            self.clear_session_context()
        log.info("cleared %s memories", count)
        return int(count)

    # ------------------------------------------------------------ retention
    def prune(self, *, stale_days: int = 365, keep_pinned: bool = True) -> int:
        """Delete memories not updated in ``stale_days`` (pinned entries kept)."""
        cutoff = to_iso(now() - timedelta(days=stale_days))
        sql = "DELETE FROM memories WHERE updated_at < ? AND use_count = 0"
        if keep_pinned:
            sql += " AND pinned = 0"
        count = self.db.execute_write(sql, (cutoff,))
        return int(count)

    # -------------------------------------------------------- export/import
    def export_payload(self, *, kinds: Optional[Sequence[str]] = None) -> dict[str, Any]:
        memories = [m for m in self.list(limit=10000, kind=None) if m.kind != KIND_SESSION]
        if kinds:
            memories = [m for m in memories if m.kind in kinds]
        return {
            "type": "lumi.memory.export",
            "version": 1,
            "exported_at": to_iso(now()),
            "memories": [m.to_dict() for m in memories],
        }

    def import_payload(self, payload: dict[str, Any], *, replace: bool = False) -> dict[str, int]:
        entries = payload.get("memories")
        if not isinstance(entries, list):
            raise ValueError("import file does not contain a 'memories' list")
        stats = {"added": 0, "updated": 0, "skipped": 0}
        if replace:
            self.clear()
        for entry in entries:
            if not isinstance(entry, dict):
                stats["skipped"] += 1
                continue
            content = normalise(str(entry.get("content", "")))
            kind = str(entry.get("kind", KIND_FACT))
            if not content or kind not in MEMORY_KINDS or kind == KIND_SESSION:
                stats["skipped"] += 1
                continue
            if looks_sensitive(content):
                stats["skipped"] += 1
                continue
            existing = self.find_by_key(str(entry.get("key", ""))) if entry.get("key") else None
            if existing and existing.id is not None:
                self.update(existing.id, content=content, kind=kind, pinned=bool(entry.get("pinned", False)))
                stats["updated"] += 1
            else:
                self.add(
                    content,
                    kind=kind,
                    key=str(entry.get("key", "")),
                    source=str(entry.get("source", "import")),
                    confidence=float(entry.get("confidence", 1.0) or 1.0),
                    pinned=bool(entry.get("pinned", False)),
                )
                stats["added"] += 1
        log.info("memory import: %s", stats)
        return stats

    def counts_by_kind(self) -> dict[str, int]:
        rows = self.db.query("SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind")
        counts = {row["kind"]: int(row["n"]) for row in rows}
        counts[KIND_SESSION] = len(self._session)
        return counts

    def total(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM memories", default=0) or 0)


class ConversationStore:
    """Conversation history, pruned according to the user's retention setting."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ lifecycle
    def start(self, *, title: str = "", source: str = "chat", model: str = "", provider: str = "local") -> Conversation:
        stamp = to_iso(now())
        conversation_id = self.db.execute_write(
            "INSERT INTO conversations(title, source, model, provider, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            (title[:200], source, model or None, provider, stamp, stamp),
        )
        row = self.db.query_one("SELECT * FROM conversations WHERE id = ?", (int(conversation_id),))
        assert row is not None
        return Conversation.from_row(row)

    def get(self, conversation_id: int) -> Optional[Conversation]:
        row = self.db.query_one("SELECT * FROM conversations WHERE id = ?", (int(conversation_id),))
        return Conversation.from_row(row) if row else None

    def latest(self) -> Optional[Conversation]:
        row = self.db.query_one(
            "SELECT * FROM conversations WHERE archived = 0 ORDER BY updated_at DESC, id DESC LIMIT 1"
        )
        return Conversation.from_row(row) if row else None

    def list(self, *, limit: int = 50, include_archived: bool = False) -> list[Conversation]:
        sql = "SELECT * FROM conversations"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        return [Conversation.from_row(row) for row in self.db.query(sql, (int(limit),))]

    def touch(self, conversation_id: int, *, model: str = "", title: str | None = None) -> None:
        fields = ["updated_at = ?"]
        params: list[Any] = [to_iso(now())]
        if model:
            fields.append("model = ?")
            params.append(model)
        if title:
            fields.append("title = ?")
            params.append(title[:200])
        params.append(int(conversation_id))
        self.db.execute_write(f"UPDATE conversations SET {', '.join(fields)} WHERE id = ?", params)

    def rename(self, conversation_id: int, title: str) -> None:
        self.db.execute_write(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title[:200], to_iso(now()), int(conversation_id)),
        )

    def delete(self, conversation_id: int) -> int:
        count = self.db.execute_write(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (int(conversation_id),)
        )
        self.db.execute_write("DELETE FROM messages WHERE conversation_id = ?", (int(conversation_id),))
        self.db.execute_write("DELETE FROM conversations WHERE id = ?", (int(conversation_id),))
        return int(count)

    def clear(self, *, keep_last: int = 0) -> int:
        ids = [row["id"] for row in self.db.query("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT -1 OFFSET ?", (int(keep_last),))]
        for conversation_id in ids:
            self.delete(int(conversation_id))
        return len(ids)

    # -------------------------------------------------------------- messages
    def add_message(
        self,
        conversation_id: Optional[int],
        role: str,
        content: str,
        *,
        model: str = "",
        provider: str = "local",
        meta: Optional[dict[str, Any]] = None,
    ) -> Message:
        stamp = to_iso(now())
        meta_json = json.dumps(meta, default=str) if meta else None
        message_id = self.db.execute_write(
            """
            INSERT INTO messages(conversation_id, role, content, model, provider, created_at, meta_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, role, content, model or None, provider, stamp, meta_json),
        )
        if conversation_id:
            self.touch(conversation_id, model=model)
            self._maybe_autotitle(conversation_id, role, content)
        row = self.db.query_one("SELECT * FROM messages WHERE id = ?", (int(message_id),))
        assert row is not None
        return Message.from_row(row)

    def _maybe_autotitle(self, conversation_id: int, role: str, content: str) -> None:
        if role != "user":
            return
        conversation = self.get(conversation_id)
        if conversation is None or conversation.title.strip():
            return
        title = normalise(content)[:60]
        if title:
            self.db.execute_write("UPDATE conversations SET title = ? WHERE id = ?", (title, int(conversation_id)))

    def messages(self, conversation_id: int, *, limit: int = 200) -> list[Message]:
        rows = self.db.query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id LIMIT ?",
            (int(conversation_id), int(limit)),
        )
        return [Message.from_row(row) for row in rows]

    def recent_messages(self, conversation_id: int, *, limit: int = 12) -> list[Message]:
        rows = self.db.query(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (int(conversation_id), int(limit)),
        )
        return [Message.from_row(row) for row in rows]

    def search_messages(self, query: str, *, limit: int = 25) -> list[Message]:
        text = normalise(query)
        if not text:
            return []
        if self.db.fts_available():
            try:
                rows = self.db.query(
                    "SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                    "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?",
                    (_fts_query(text), int(limit)),
                )
                if rows:
                    return [Message.from_row(row) for row in rows]
            except sqlite3.OperationalError:
                pass
        rows = self.db.query(
            "SELECT * FROM messages WHERE lower(content) LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{text.lower()}%", int(limit)),
        )
        return [Message.from_row(row) for row in rows]

    def message_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM messages", default=0) or 0)

    # ------------------------------------------------------------ retention
    def prune(self, *, retention_days: int) -> int:
        """Apply the retention policy.

        ``0`` keeps everything; a positive value deletes messages and
        conversations older than that many days.  A month-based alternative
        ("keep 3 months") is handled by :meth:`prune_months`.
        """
        if retention_days <= 0:
            return 0
        cutoff = to_iso(now() - timedelta(days=int(retention_days)))
        stale_ids = [
            int(row["id"])
            for row in self.db.query("SELECT id FROM conversations WHERE updated_at < ?", (cutoff,))
        ]
        for conversation_id in stale_ids:
            self.delete(conversation_id)
        orphan_messages = self.db.execute_write(
            "DELETE FROM messages WHERE created_at < ? AND conversation_id IS NULL", (cutoff,)
        )
        return len(stale_ids) + int(orphan_messages or 0)

    def prune_months(self, months: int) -> int:
        if months <= 0:
            return 0
        cutoff = to_iso(add_months(now(), -months))
        stale_ids = [
            int(row["id"])
            for row in self.db.query("SELECT id FROM conversations WHERE updated_at < ?", (cutoff,))
        ]
        for conversation_id in stale_ids:
            self.delete(conversation_id)
        return len(stale_ids)

    # -------------------------------------------------------------- export
    def export_payload(self, conversation_id: int) -> dict[str, Any]:
        conversation = self.get(conversation_id)
        if conversation is None:
            raise ValueError(f"conversation {conversation_id} not found")
        messages = self.messages(conversation_id, limit=10000)
        return {
            "type": "lumi.conversation.export",
            "version": 1,
            "conversation": {
                "title": conversation.title,
                "source": conversation.source,
                "model": conversation.model,
                "created_at": to_iso(conversation.created_at),
            },
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "created_at": to_iso(message.created_at),
                    "model": message.model,
                }
                for message in messages
            ],
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_SAFE_ORDER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*( (ASC|DESC))?(, [a-zA-Z_][a-zA-Z0-9_]*( (ASC|DESC))?)*$")


def _safe_order(order: str) -> str:
    """Whitelist ORDER BY clauses to keep f-strings out of SQL injection range."""
    candidate = order.strip()
    return candidate if _SAFE_ORDER.match(candidate) else "updated_at DESC, id DESC"


def _fts_query(text: str) -> str:
    """Build a forgiving FTS5 query: quote each token, AND them together."""
    tokens = [token for token in re.split(r"[^\w]+", text) if token]
    if not tokens:
        return '""'
    return " ".join(f'"{token}"' for token in tokens[:12])


def format_memory_lines(memories: Iterable[Memory], *, max_chars: int = 1200) -> list[str]:
    """Render memories for the prompt, redacting and truncating defensively."""
    lines: list[str] = []
    used = 0
    for memory in memories:
        line = redact_text(memory.prompt_line(), max_length=300)
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return lines
