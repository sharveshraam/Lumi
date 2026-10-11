"""Focus sessions.

A focus session is a plain record of "I worked on this from X to Y".  LUMI does
not block applications or hide windows - the user asked for a study aid, not a
nanny - so this is bookkeeping plus a timer the UI can show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

from lumi.memory.store import Database
from lumi.utils.timeutil import now, to_iso

log = logging.getLogger("lumi.reminders.focus")

MAX_LABEL_CHARS = 80
MAX_SESSION_MINUTES = 480


@dataclass
class FocusSession:
    """One completed or running focus block."""

    id: Optional[int]
    label: str = ""
    task_id: Optional[int] = None
    started_at: datetime = None  # type: ignore[assignment]
    ended_at: Optional[datetime] = None
    planned_minutes: int = 0
    completed: bool = False
    note: str = ""
    created_at: Optional[datetime] = None

    @property
    def running(self) -> bool:
        return self.ended_at is None

    @property
    def actual_minutes(self) -> int:
        end = self.ended_at or now()
        if self.started_at is None:
            return 0
        return max(0, int((end - self.started_at).total_seconds() // 60))

    @property
    def remaining_minutes(self) -> int:
        return max(0, self.planned_minutes - self.actual_minutes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "task_id": self.task_id,
            "started_at": to_iso(self.started_at) if self.started_at else "",
            "ended_at": to_iso(self.ended_at) if self.ended_at else "",
            "planned_minutes": self.planned_minutes,
            "actual_minutes": self.actual_minutes,
            "running": self.running,
            "completed": self.completed,
            "note": self.note,
        }

    @classmethod
    def from_row(cls, row: Any) -> "FocusSession":
        from lumi.utils.timeutil import parse_iso

        return cls(
            id=int(row["id"]),
            label=str(row["label"] or ""),
            task_id=int(row["task_id"]) if row["task_id"] else None,
            started_at=parse_iso(row["started_at"]) or now(),
            ended_at=parse_iso(row["ended_at"]),
            planned_minutes=int(row["planned_minutes"] or 0),
            completed=bool(row["completed"]),
            note=str(row["notes"] or ""),
        )


class FocusRepository:
    """Stores focus sessions in the application database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ write
    def start(
        self,
        label: str = "",
        *,
        task_id: Optional[int] = None,
        planned_minutes: int = 25,
        started_at: Optional[datetime] = None,
    ) -> FocusSession:
        if self.active() is not None:
            raise ValueError("a focus session is already running")
        minutes = max(1, min(int(planned_minutes or 25), MAX_SESSION_MINUTES))
        stamp = started_at or now()
        session_id = self.db.execute_write(
            """
            INSERT INTO focus_sessions(label, task_id, started_at, planned_minutes, completed, notes)
            VALUES(?, ?, ?, ?, 0, '')
            """,
            ((label or "")[:MAX_LABEL_CHARS], task_id, to_iso(stamp), minutes),
        )
        return self.get(int(session_id))  # type: ignore[return-value]

    def finish(
        self,
        session_id: Optional[int] = None,
        *,
        completed: bool = True,
        notes: str = "",
        note: str = "",
        ended_at: Optional[datetime] = None,
    ) -> Optional[FocusSession]:
        session = self.get(session_id) if session_id else self.active()
        if session is None or session.id is None:
            return None
        self.db.execute_write(
            "UPDATE focus_sessions SET ended_at = ?, completed = ?, notes = ? WHERE id = ?",
            (to_iso(ended_at or now()), 1 if completed else 0, (notes or note or "")[:500], int(session.id)),
        )
        return self.get(int(session.id))

    def clear(self) -> int:
        return int(self.db.execute_write("DELETE FROM focus_sessions") or 0)

    # ------------------------------------------------------------------- read
    def get(self, session_id: Optional[int]) -> Optional[FocusSession]:
        if session_id is None:
            return None
        row = self.db.query_one("SELECT * FROM focus_sessions WHERE id = ?", (int(session_id),))
        return FocusSession.from_row(row) if row else None

    def active(self) -> Optional[FocusSession]:
        row = self.db.query_one("SELECT * FROM focus_sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
        return FocusSession.from_row(row) if row else None

    def list(self, *, limit: int = 20, since: Optional[datetime] = None) -> list[FocusSession]:
        sql = "SELECT * FROM focus_sessions"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE started_at >= ?"
            params.append(to_iso(since))
        sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [FocusSession.from_row(row) for row in self.db.query(sql, tuple(params))]

    def today_minutes(self, *, reference: Optional[datetime] = None) -> int:
        day = (reference or now()).date()
        return self._minutes_between(day, day)

    def week_minutes(self, *, reference: Optional[datetime] = None) -> int:
        moment = reference or now()
        start = moment.date() - timedelta(days=moment.weekday())
        return self._minutes_between(start, moment.date())

    def week_stats(self, *, reference: Optional[datetime] = None, days: int = 7) -> dict[str, Any]:
        """Focus totals for the last ``days`` days.

        Returns both the summary numbers (used by the tools and the status bar)
        and the per-day breakdown (used by the focus chart).
        """
        moment = (reference or now()).date()
        span = max(1, int(days))
        per_day: list[dict[str, Any]] = []
        all_sessions: list[FocusSession] = []
        for offset in range(span - 1, -1, -1):
            day = moment - timedelta(days=offset)
            sessions = [
                session
                for session in self.list(limit=500, since=datetime.combine(day, datetime.min.time()))
                if session.started_at and session.started_at.date() == day
            ]
            closed = [session for session in sessions if not session.running]
            all_sessions.extend(closed)
            per_day.append(
                {
                    "date": day.isoformat(),
                    "weekday": day.strftime("%a"),
                    "minutes": sum(session.actual_minutes for session in closed),
                    "sessions": len(closed),
                }
            )
        total = sum(session.actual_minutes for session in all_sessions)
        by_label: dict[str, int] = {}
        for session in all_sessions:
            if session.label:
                by_label[session.label] = by_label.get(session.label, 0) + session.actual_minutes
        return {
            "days": per_day,
            "sessions": len(all_sessions),
            "total_minutes": total,
            "average_minutes": round(total / len(all_sessions)) if all_sessions else 0,
            "by_label": by_label,
            "span_days": span,
            "started_at": per_day[0]["date"] if per_day else "",
            "ended_at": per_day[-1]["date"] if per_day else "",
        }

    def week_minutes_total(self, *, reference: Optional[datetime] = None) -> int:
        return int(self.week_stats(reference=reference, days=7)["total_minutes"])

    def totals(self, *, since: Optional[datetime] = None) -> dict[str, Any]:
        sessions = self.list(limit=1000, since=since)
        closed = [session for session in sessions if not session.running]
        minutes = sum(session.actual_minutes for session in closed)
        labels: dict[str, int] = {}
        for session in closed:
            if session.label:
                labels[session.label] = labels.get(session.label, 0) + session.actual_minutes
        top = sorted(labels.items(), key=lambda item: item[1], reverse=True)[:5]
        return {
            "sessions": len(closed),
            "minutes": minutes,
            "hours": round(minutes / 60, 1),
            "average_minutes": round(minutes / len(closed)) if closed else 0,
            "top_labels": [{"label": label, "minutes": value} for label, value in top],
        }

    # ---------------------------------------------------------------- helpers
    def _minutes_between(self, start: date, end: date) -> int:
        rows = self.db.query(
            "SELECT started_at, ended_at FROM focus_sessions WHERE started_at >= ? AND started_at < ?",
            (to_iso(datetime.combine(start, datetime.min.time())), to_iso(datetime.combine(end + timedelta(days=1), datetime.min.time()))),
        )
        from lumi.utils.timeutil import parse_iso

        total = 0
        for row in rows:
            began = parse_iso(row["started_at"])
            finished = parse_iso(row["ended_at"]) or now()
            if began is None:
                continue
            total += max(0, int((finished - began).total_seconds() // 60))
        return total
