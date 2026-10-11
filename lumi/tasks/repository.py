"""Task persistence and queries."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from lumi.memory.store import Database
from lumi.tasks import recurrence as recurrence_rules
from lumi.tasks.models import OPEN_STATUSES, PRIORITIES, STATUSES, Task
from lumi.utils.timeutil import end_of_day, now, start_of_day, to_iso

log = logging.getLogger("lumi.tasks")

MAX_TITLE = 300
MAX_NOTES = 20000


class TaskRepository:
    """CRUD plus the queries the UI and the daily briefing need."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ write
    def create(
        self,
        title: str,
        *,
        notes: str = "",
        category: str = "general",
        priority: str = "medium",
        due_at: datetime | None = None,
        remind_at: datetime | None = None,
        recurrence: str = "none",
        recurrence_end: datetime | None = None,
        tags: Sequence[str] | None = None,
        parent_id: int | None = None,
        source: str = "manual",
    ) -> Task:
        clean_title = " ".join((title or "").split())
        if not clean_title:
            raise ValueError("task title cannot be empty")
        if len(clean_title) > MAX_TITLE:
            raise ValueError(f"task title is too long (limit {MAX_TITLE} characters)")
        if priority not in PRIORITIES:
            priority = "medium"
        stamp = to_iso(now())
        task_id = self.db.execute_write(
            """
            INSERT INTO tasks(title, notes, category, priority, status, due_at, remind_at,
                              created_at, updated_at, recurrence, recurrence_end, parent_id, tags, source)
            VALUES(?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_title,
                (notes or "")[:MAX_NOTES],
                (category or "general").strip()[:60],
                priority,
                to_iso(due_at),
                to_iso(remind_at),
                stamp,
                stamp,
                recurrence_rules.normalise(recurrence),
                to_iso(recurrence_end),
                parent_id,
                ",".join(tag.strip() for tag in (tags or []) if tag.strip())[:300],
                source,
            ),
        )
        created = self.get(int(task_id))
        assert created is not None
        log.info("task created (id=%s, title=%r)", created.id, created.title[:60])
        return created

    def update(self, task_id: int, **changes: Any) -> Optional[Task]:
        allowed = {
            "title", "notes", "category", "priority", "status",
            "due_at", "remind_at", "recurrence", "recurrence_end", "tags",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "title":
                clean = " ".join(str(value).split())
                if not clean:
                    raise ValueError("task title cannot be empty")
                fields.append("title = ?")
                params.append(clean[:MAX_TITLE])
            elif key == "notes":
                fields.append("notes = ?")
                params.append(str(value)[:MAX_NOTES])
            elif key == "priority":
                fields.append("priority = ?")
                params.append(value if value in PRIORITIES else "medium")
            elif key == "status":
                if value not in STATUSES:
                    raise ValueError(f"invalid status: {value!r}")
                fields.append("status = ?")
                params.append(value)
                fields.append("completed_at = ?")
                params.append(to_iso(now()) if value == "done" else None)
            elif key in {"due_at", "remind_at", "recurrence_end"}:
                fields.append(f"{key} = ?")
                params.append(to_iso(value) if isinstance(value, datetime) else (value or None))
            elif key == "recurrence":
                fields.append("recurrence = ?")
                params.append(recurrence_rules.normalise(str(value)))
            elif key == "category":
                fields.append("category = ?")
                params.append(str(value).strip()[:60] or "general")
            elif key == "tags":
                if isinstance(value, str):
                    tags = [part.strip() for part in value.split(",") if part.strip()]
                else:
                    tags = [str(part).strip() for part in (value or []) if str(part).strip()]
                fields.append("tags = ?")
                params.append(",".join(tags)[:300])

        if not fields:
            return self.get(task_id)
        fields.append("updated_at = ?")
        params.append(to_iso(now()))
        params.append(int(task_id))
        self.db.execute_write(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params)
        return self.get(int(task_id))

    def complete(self, task_id: int, *, completed: bool = True) -> Optional[Task]:
        task = self.update(task_id, status="done" if completed else "pending")
        if completed and task and task.recurrence != "none":
            self._spawn_next_occurrence(task)
        return task

    def _spawn_next_occurrence(self, task: Task) -> Optional[Task]:
        """Create the next instance of a repeating task (called on completion)."""
        reference = task.due_at or now()
        nxt_due = recurrence_rules.next_from_now(task.recurrence, now(), anchor=reference)
        if nxt_due is None:
            return None
        if task.recurrence_end and nxt_due > task.recurrence_end:
            log.info("recurrence for task %s finished at %s", task.id, to_iso(task.recurrence_end))
            return None
        delta = nxt_due - reference
        new_remind = (task.remind_at + delta) if task.remind_at else None
        spawned = self.create(
            task.title,
            notes=task.notes,
            category=task.category,
            priority=task.priority,
            due_at=nxt_due,
            remind_at=new_remind,
            recurrence=task.recurrence,
            recurrence_end=task.recurrence_end,
            tags=task.tags,
            parent_id=task.id,
            source=task.source,
        )
        log.info("recurring task %s -> new task %s due %s", task.id, spawned.id, to_iso(nxt_due))
        return spawned

    def postpone(self, task_id: int, *, until: datetime | None = None, days: int | None = None, minutes: int | None = None) -> Optional[Task]:
        task = self.get(task_id)
        if task is None:
            return None
        base = task.due_at or now()
        if until is not None:
            new_due = until
        elif days is not None:
            new_due = base + timedelta(days=days)
        elif minutes is not None:
            new_due = base + timedelta(minutes=minutes)
        else:
            new_due = base + timedelta(days=1)
        remind = task.remind_at
        if remind and task.due_at:
            reminder_offset = task.due_at - remind
            remind = new_due - reminder_offset
        elif remind and remind < now():
            remind = new_due
        return self.update(task_id, due_at=new_due, remind_at=remind, status="pending")

    def set_status(self, task_id: int, status: str) -> Optional[Task]:
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        return self.update(task_id, status=status)

    def delete(self, task_id: int) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
            return cursor.rowcount > 0

    def delete_completed(self, *, older_than_days: int = 0) -> int:
        if older_than_days > 0:
            cutoff = to_iso(now() - timedelta(days=older_than_days))
            return int(self.db.execute_write("DELETE FROM tasks WHERE status = 'done' AND completed_at < ?", (cutoff,)) or 0)
        return int(self.db.execute_write("DELETE FROM tasks WHERE status = 'done'") or 0)

    def clear_all(self) -> int:
        return int(self.db.execute_write("DELETE FROM tasks") or 0)

    # ------------------------------------------------------------------- read
    def get(self, task_id: int) -> Optional[Task]:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (int(task_id),))
        return Task.from_row(row) if row else None

    def find_by_title(self, title: str, *, open_only: bool = True) -> list[Task]:
        like = f"%{title.strip().lower()}%"
        sql = "SELECT * FROM tasks WHERE lower(title) LIKE ?"
        params: list[Any] = [like]
        if open_only:
            sql += " AND status IN ('pending', 'in_progress')"
        sql += " ORDER BY updated_at DESC LIMIT 10"
        return [Task.from_row(row) for row in self.db.query(sql, params)]

    def list(
        self,
        *,
        status: str | Sequence[str] | None = None,
        category: str | None = None,
        priority: str | None = None,
        search: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        limit: int = 500,
        order: str = "due",
    ) -> list[Task]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if status:
            if isinstance(status, str):
                if status == "open":
                    statuses = list(OPEN_STATUSES)
                elif status == "all":
                    statuses = list(STATUSES)
                else:
                    statuses = [status]
            else:
                statuses = list(status)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        if category:
            sql += " AND lower(category) = lower(?)"
            params.append(category)
        if priority:
            sql += " AND priority = ?"
            params.append(priority)
        if search:
            sql += " AND (lower(title) LIKE ? OR lower(notes) LIKE ? OR lower(tags) LIKE ?)"
            like = f"%{search.strip().lower()}%"
            params.extend([like, like, like])
        if due_before:
            sql += " AND due_at IS NOT NULL AND due_at < ?"
            params.append(to_iso(due_before))
        if due_after:
            sql += " AND due_at IS NOT NULL AND due_at >= ?"
            params.append(to_iso(due_after))

        orders = {
            "due": "CASE WHEN status IN ('pending','in_progress') THEN 0 ELSE 1 END, "
                   "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, "
                   "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id",
            "created": "created_at DESC, id DESC",
            "updated": "updated_at DESC, id DESC",
            "priority": "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_at",
            "title": "lower(title) ASC",
        }
        sql += f" ORDER BY {orders.get(order, orders['due'])} LIMIT ?"
        params.append(int(limit))
        return [Task.from_row(row) for row in self.db.query(sql, params)]

    # ------------------------------------------------------------ groupings
    def overdue(self, *, reference: datetime | None = None, limit: int = 100) -> list[Task]:
        ref = reference or now()
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND due_at IS NOT NULL AND due_at < ? "
            "ORDER BY due_at LIMIT ?",
            (to_iso(ref), int(limit)),
        )
        return [Task.from_row(row) for row in rows]

    def due_today(self, *, reference: datetime | None = None) -> list[Task]:
        ref = reference or now()
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND due_at >= ? AND due_at <= ? "
            "ORDER BY due_at, CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END",
            (to_iso(start_of_day(ref)), to_iso(end_of_day(ref))),
        )
        return [Task.from_row(row) for row in rows]

    def due_between(self, start: datetime, end: datetime, *, include_overdue: bool = False) -> list[Task]:
        if include_overdue:
            rows = self.db.query(
                "SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND due_at IS NOT NULL AND due_at <= ? "
                "ORDER BY due_at",
                (to_iso(end),),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND due_at >= ? AND due_at <= ? "
                "ORDER BY due_at",
                (to_iso(start), to_iso(end)),
            )
        return [Task.from_row(row) for row in rows]

    def upcoming(self, *, days: int = 7, reference: datetime | None = None, limit: int = 100) -> list[Task]:
        ref = reference or now()
        end = end_of_day(ref + timedelta(days=days))
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND due_at > ? AND due_at <= ? "
            "ORDER BY due_at LIMIT ?",
            (to_iso(ref), to_iso(end), int(limit)),
        )
        return [Task.from_row(row) for row in rows]

    def without_due_date(self, *, limit: int = 50) -> list[Task]:
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND due_at IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
        return [Task.from_row(row) for row in rows]

    def next_reminders(self, *, limit: int = 20) -> list[Task]:
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND remind_at IS NOT NULL AND remind_at >= ? "
            "ORDER BY remind_at LIMIT ?",
            (to_iso(now()), int(limit)),
        )
        return [Task.from_row(row) for row in rows]

    def categories(self) -> list[tuple[str, int]]:
        rows = self.db.query(
            "SELECT category, COUNT(*) AS n FROM tasks WHERE status IN ('pending','in_progress') "
            "GROUP BY category ORDER BY n DESC, category"
        )
        return [(row["category"], int(row["n"])) for row in rows]

    def stats(self, *, reference: datetime | None = None) -> dict[str, int]:
        ref = reference or now()
        return {
            "open": int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE status IN ('pending','in_progress')", default=0) or 0),
            "done_today": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND completed_at >= ?",
                    (to_iso(start_of_day(ref)),),
                    default=0,
                )
                or 0
            ),
            "overdue": len(self.overdue(reference=ref, limit=1000)),
            "due_today": len(self.due_today(reference=ref)),
            "total": int(self.db.scalar("SELECT COUNT(*) FROM tasks", default=0) or 0),
        }

    def search(self, query: str, *, limit: int = 50) -> list[Task]:
        text = (query or "").strip().lower()
        if not text:
            return []
        like = f"%{text}%"
        rows = self.db.query(
            "SELECT * FROM tasks WHERE lower(title) LIKE ? OR lower(notes) LIKE ? OR lower(tags) LIKE ? "
            "ORDER BY CASE WHEN status IN ('pending','in_progress') THEN 0 ELSE 1 END, due_at LIMIT ?",
            (like, like, like, int(limit)),
        )
        return [Task.from_row(row) for row in rows]

    def suggest_order(self, *, reference: datetime | None = None, limit: int = 12) -> list[Task]:
        """Ordered suggestion for the daily briefing (deadline + priority aware)."""
        ref = reference or now()
        candidates: list[Task] = []
        seen: set[int] = set()
        for group in (self.overdue(reference=ref), self.due_today(reference=ref), self.upcoming(days=7, reference=ref)):
            for task in group:
                if task.id in seen:
                    continue
                seen.add(task.id)  # type: ignore[arg-type]
                candidates.append(task)

        def score(task: Task) -> tuple[int, float, int]:
            if task.due_at:
                hours = (task.due_at - ref).total_seconds() / 3600
            else:
                hours = 24 * 14
            # Anything already overdue jumps the queue; then urgency, then priority.
            overdue_penalty = -1000 if hours < 0 else 0
            return (overdue_penalty, hours, task.priority_rank())

        candidates.sort(key=score)
        if len(candidates) < limit:
            for task in self.without_due_date(limit=limit):
                if task.id in seen:
                    continue
                seen.add(task.id)  # type: ignore[arg-type]
                candidates.append(task)
                if len(candidates) >= limit:
                    break
        return candidates[:limit]

    def link_ambiguous_titles(self, needle: str) -> list[Task]:
        """Helper used by the assistant when the user says 'the report task'."""
        words = [w for w in needle.lower().split() if len(w) > 3]
        if not words:
            return []
        clauses = " OR ".join("lower(title) LIKE ?" for _ in words)
        params = [f"%{word}%" for word in words]
        rows = self.db.query(
            f"SELECT * FROM tasks WHERE status IN ('pending','in_progress') AND ({clauses}) LIMIT 10", params
        )
        return [Task.from_row(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}


def summarise(tasks: Iterable[Task], *, reference: datetime | None = None, limit: int = 10) -> str:
    """Compact multi-line text used in tool results and the briefing."""
    ref = reference or now()
    lines = []
    for index, task in enumerate(list(tasks)[:limit], start=1):
        due = task.human_due(ref)
        flags = []
        if task.is_overdue(ref):
            flags.append("OVERDUE")
        if task.recurrence != "none":
            flags.append(recurrence_rules.describe(task.recurrence))
        if task.priority in {"high", "urgent"}:
            flags.append(task.priority.upper())
        suffix = f" [{' · '.join(flags)}]" if flags else ""
        lines.append(f"{index}. #{task.id} {task.title} - {due}{suffix}")
    return "\n".join(lines) if lines else "No tasks."
