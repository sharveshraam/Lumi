"""Dataclass views of stored rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from lumi.utils.timeutil import from_iso

# ---------------------------------------------------------------- memory kinds

KIND_PREFERENCE = "preference"
KIND_FACT = "fact"
KIND_PROJECT = "project"
KIND_SESSION = "session"  # never persisted; kept only for the running session

MEMORY_KINDS: tuple[str, ...] = (KIND_PREFERENCE, KIND_FACT, KIND_PROJECT, KIND_SESSION)

KIND_LABELS: dict[str, str] = {
    KIND_PREFERENCE: "Preference",
    KIND_FACT: "Personal fact",
    KIND_PROJECT: "Project context",
    KIND_SESSION: "This session only",
}

MEMORY_KIND_DESCRIPTIONS: dict[str, str] = {
    KIND_PREFERENCE: "How you like LUMI to behave (tone, format, defaults).",
    KIND_FACT: "Something durable about you that you asked LUMI to remember.",
    KIND_PROJECT: "Context for a specific project, subject or course.",
    KIND_SESSION: "Temporary context used until the app is closed. Never written to disk.",
}


def _dt(value: Any) -> Optional[datetime]:
    return from_iso(value) if isinstance(value, str) else value


@dataclass
class Memory:
    id: Optional[int] = None
    kind: str = KIND_FACT
    content: str = ""
    key: str = ""
    source: str = "manual"
    confidence: float = 1.0
    pinned: bool = False
    use_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Any) -> "Memory":
        return cls(
            id=row["id"],
            kind=row["kind"],
            content=row["content"],
            key=row["key"] or "",
            source=row["source"] or "manual",
            confidence=float(row["confidence"] or 1.0),
            pinned=bool(row["pinned"]),
            use_count=int(row["use_count"] or 0),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            last_used_at=_dt(row["last_used_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "key": self.key,
            "source": self.source,
            "confidence": self.confidence,
            "pinned": self.pinned,
            "use_count": self.use_count,
            "created_at": self.created_at.isoformat(sep=" ") if self.created_at else None,
            "updated_at": self.updated_at.isoformat(sep=" ") if self.updated_at else None,
            "last_used_at": self.last_used_at.isoformat(sep=" ") if self.last_used_at else None,
        }

    def prompt_line(self) -> str:
        prefix = {
            KIND_PREFERENCE: "prefers",
            KIND_PROJECT: "project",
            KIND_SESSION: "this session",
            KIND_FACT: "fact",
        }.get(self.kind, "note")
        if self.key:
            return f"[{prefix}: {self.key}] {self.content}"
        return f"[{prefix}] {self.content}"


@dataclass
class Conversation:
    id: Optional[int] = None
    title: str = ""
    source: str = "chat"
    model: str = ""
    provider: str = "local"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    archived: bool = False

    @classmethod
    def from_row(cls, row: Any) -> "Conversation":
        return cls(
            id=row["id"],
            title=row["title"] or "",
            source=row["source"] or "chat",
            model=row["model"] or "",
            provider=row["provider"] or "local",
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            archived=bool(row["archived"]),
        )


@dataclass
class Message:
    id: Optional[int] = None
    conversation_id: Optional[int] = None
    role: str = "user"
    content: str = ""
    model: str = ""
    provider: str = "local"
    created_at: Optional[datetime] = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Any) -> "Message":
        import json

        meta: dict[str, Any] = {}
        raw_meta = row["meta_json"] if "meta_json" in row.keys() else None
        if raw_meta:
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    meta = parsed
            except (ValueError, TypeError):
                meta = {}
        return cls(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            content=row["content"] or "",
            model=row["model"] or "",
            provider=row["provider"] or "local",
            created_at=_dt(row["created_at"]),
            meta=meta,
        )

    def to_chat_dict(self) -> dict[str, str]:
        """Shape expected by the Ollama ``/api/chat`` endpoint."""
        return {"role": self.role, "content": self.content}
