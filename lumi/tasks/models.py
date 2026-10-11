"""Task value objects and enumerations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from lumi.utils.timeutil import from_iso, to_iso

PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")
PRIORITY_ORDER: dict[str, int] = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

STATUSES: tuple[str, ...] = ("pending", "in_progress", "done", "cancelled")
OPEN_STATUSES: tuple[str, ...] = ("pending", "in_progress")

STATUS_LABELS: dict[str, str] = {
    "pending": "To do",
    "in_progress": "In progress",
    "done": "Done",
    "cancelled": "Cancelled",
}


def _dt(value: Any) -> Optional[datetime]:
    return from_iso(value) if isinstance(value, str) else value


@dataclass
class Task:
    id: Optional[int] = None
    title: str = ""
    notes: str = ""
    category: str = "general"
    priority: str = "medium"
    status: str = "pending"
    due_at: Optional[datetime] = None
    remind_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    recurrence: str = "none"
    recurrence_end: Optional[datetime] = None
    parent_id: Optional[int] = None
    tags: list[str] = field(default_factory=list)
    source: str = "manual"

    # -------------------------------------------------------------- mapping
    @classmethod
    def from_row(cls, row: Any) -> "Task":
        tags_raw = row["tags"] or ""
        return cls(
            id=row["id"],
            title=row["title"],
            notes=row["notes"] or "",
            category=row["category"] or "general",
            priority=row["priority"] if row["priority"] in PRIORITIES else "medium",
            status=row["status"] if row["status"] in STATUSES else "pending",
            due_at=_dt(row["due_at"]),
            remind_at=_dt(row["remind_at"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            completed_at=_dt(row["completed_at"]),
            recurrence=row["recurrence"] or "none",
            recurrence_end=_dt(row["recurrence_end"]),
            parent_id=row["parent_id"],
            tags=[tag.strip() for tag in tags_raw.split(",") if tag.strip()],
            source=row["source"] or "manual",
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "notes": self.notes,
            "category": self.category,
            "priority": self.priority,
            "status": self.status,
            "due_at": to_iso(self.due_at),
            "remind_at": to_iso(self.remind_at),
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
            "completed_at": to_iso(self.completed_at),
            "recurrence": self.recurrence,
            "recurrence_end": to_iso(self.recurrence_end),
            "parent_id": self.parent_id,
            "tags": ",".join(self.tags),
            "source": self.source,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.to_row()
        data["id"] = self.id
        data["tags"] = list(self.tags)
        return data

    # ------------------------------------------------------------- helpers
    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    def is_overdue(self, reference: datetime) -> bool:
        return bool(self.due_at and self.is_open and self.due_at < reference)

    def priority_rank(self) -> int:
        return PRIORITY_ORDER.get(self.priority, 2)

    def sort_key(self, reference: datetime) -> tuple[int, float, int, int]:
        """Order for lists and briefings: overdue first, then due time, then priority."""
        opened = 0 if self.is_open else 1
        overdue = 0 if self.is_overdue(reference) else 1
        due_rank = self.due_at.timestamp() if self.due_at else float("inf")
        return (opened, overdue, due_rank, self.priority_rank())

    def human_due(self, reference: datetime) -> str:
        from lumi.utils.timeutil import humanise_due

        return humanise_due(self.due_at, reference=reference)
