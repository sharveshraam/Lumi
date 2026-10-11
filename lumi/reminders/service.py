"""Reminder storage and the background scheduler.

Two things make reminders trustworthy:

* **They survive restarts.**  Every reminder is a row in SQLite; the scheduler
  reloads them at startup.
* **Missed ones are explained, not hidden.**  If the laptop was asleep or LUMI
  was closed when a reminder was due, the reminder fires late and says so
  ("this was due 2 hours ago, your computer was probably asleep").

The scheduler ticks on a background thread.  A tick is cheap: one indexed query
and at most a few notifications.  Nothing polls in a tight loop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Sequence

from lumi.memory.store import Database
from lumi.tasks import recurrence as recurrence_rules
from lumi.utils.timeutil import (
    from_iso,
    humanise_delta,
    humanise_due,
    now,
    to_iso,
)

log = logging.getLogger("lumi.reminders")

DEFAULT_TICK_SECONDS = 15.0
MAX_CATCHUP_PER_TICK = 5
LATE_GRACE_SECONDS = 60


@dataclass
class Reminder:
    id: Optional[int] = None
    title: str = ""
    body: str = ""
    fire_at: Optional[datetime] = None
    recurrence: str = "none"
    enabled: bool = True
    task_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_fired_at: Optional[datetime] = None
    missed_count: int = 0

    @classmethod
    def from_row(cls, row: Any) -> "Reminder":
        return cls(
            id=row["id"],
            title=row["title"] or "",
            body=row["body"] or "",
            fire_at=from_iso(row["fire_at"]),
            recurrence=row["recurrence"] or "none",
            enabled=bool(row["enabled"]),
            task_id=row["task_id"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
            last_fired_at=from_iso(row["last_fired_at"]) if row["last_fired_at"] else None,
            missed_count=int(row["missed_count"] or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "fire_at": to_iso(self.fire_at),
            "fire_at_human": humanise_due(self.fire_at),
            "recurrence": self.recurrence,
            "recurrence_text": recurrence_rules.describe(self.recurrence),
            "enabled": self.enabled,
            "task_id": self.task_id,
            "last_fired_at": to_iso(self.last_fired_at),
            "missed_count": self.missed_count,
        }

    @property
    def repeats(self) -> bool:
        return self.recurrence not in ("", "none")


@dataclass
class ReminderDelivery:
    """What the UI needs in order to show a reminder."""

    reminder: Reminder
    scheduled_for: datetime
    delivered_at: datetime
    late_by_seconds: int
    message: str

    @property
    def was_late(self) -> bool:
        return self.late_by_seconds > LATE_GRACE_SECONDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminder": self.reminder.to_dict(),
            "scheduled_for": to_iso(self.scheduled_for),
            "delivered_at": to_iso(self.delivered_at),
            "late_by_seconds": self.late_by_seconds,
            "was_late": self.was_late,
            "message": self.message,
        }


class ReminderRepository:
    """CRUD for reminders plus their delivery log."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------------- write
    def create(
        self,
        title: str,
        fire_at: datetime,
        *,
        body: str = "",
        recurrence: str = "none",
        task_id: int | None = None,
        enabled: bool = True,
    ) -> Reminder:
        clean_title = " ".join((title or "").split())
        if not clean_title:
            raise ValueError("a reminder needs a title")
        if fire_at is None:
            raise ValueError("a reminder needs a time")
        stamp = to_iso(now())
        reminder_id = self.db.execute_write(
            """
            INSERT INTO reminders(task_id, title, body, fire_at, recurrence, enabled, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, clean_title[:300], (body or "")[:2000], to_iso(fire_at), recurrence_rules.normalise(recurrence), int(enabled), stamp, stamp),
        )
        reminder = self.get(int(reminder_id))
        assert reminder is not None
        log.info("reminder created (id=%s, at=%s)", reminder.id, to_iso(fire_at))
        return reminder

    def update(self, reminder_id: int, **changes: Any) -> Optional[Reminder]:
        fields: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key == "title" and value:
                fields.append("title = ?")
                params.append(" ".join(str(value).split())[:300])
            elif key == "body":
                fields.append("body = ?")
                params.append(str(value)[:2000])
            elif key == "fire_at" and isinstance(value, datetime):
                fields.append("fire_at = ?")
                params.append(to_iso(value))
            elif key == "recurrence":
                fields.append("recurrence = ?")
                params.append(recurrence_rules.normalise(str(value)))
            elif key == "enabled":
                fields.append("enabled = ?")
                params.append(int(bool(value)))
            elif key == "last_fired_at":
                fields.append("last_fired_at = ?")
                params.append(to_iso(value) if isinstance(value, datetime) else value)
        if not fields:
            return self.get(reminder_id)
        fields.append("updated_at = ?")
        params.append(to_iso(now()))
        params.append(int(reminder_id))
        self.db.execute_write(f"UPDATE reminders SET {', '.join(fields)} WHERE id = ?", params)
        return self.get(int(reminder_id))

    def delete(self, reminder_id: int) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM reminders WHERE id = ?", (int(reminder_id),))
            return cursor.rowcount > 0

    def clear(self, *, include_fired: bool = True) -> int:
        if include_fired:
            return int(self.db.execute_write("DELETE FROM reminders") or 0)
        return int(self.db.execute_write("DELETE FROM reminders WHERE enabled = 1 AND fire_at >= ?", (to_iso(now()),)) or 0)

    # ----------------------------------------------------------------- read
    def get(self, reminder_id: int) -> Optional[Reminder]:
        row = self.db.query_one("SELECT * FROM reminders WHERE id = ?", (int(reminder_id),))
        return Reminder.from_row(row) if row else None

    def list(
        self,
        *,
        include_disabled: bool = True,
        limit: int = 200,
        order: str = "fire_at",
    ) -> list[Reminder]:
        sql = "SELECT * FROM reminders"
        params: list[Any] = []
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(int(limit))
        return [Reminder.from_row(row) for row in self.db.query(sql, params)]

    def upcoming(self, *, limit: int = 20, reference: datetime | None = None) -> list[Reminder]:
        ref = reference or now()
        rows = self.db.query(
            "SELECT * FROM reminders WHERE enabled = 1 AND fire_at >= ? ORDER BY fire_at LIMIT ?",
            (to_iso(ref), int(limit)),
        )
        return [Reminder.from_row(row) for row in rows]

    def due(self, *, reference: datetime | None = None, limit: int = MAX_CATCHUP_PER_TICK) -> list[Reminder]:
        ref = reference or now()
        rows = self.db.query(
            "SELECT * FROM reminders WHERE enabled = 1 AND fire_at <= ? ORDER BY fire_at LIMIT ?",
            (to_iso(ref), int(limit)),
        )
        return [Reminder.from_row(row) for row in rows]

    def missed_before(self, moment: datetime) -> list[Reminder]:
        rows = self.db.query(
            "SELECT * FROM reminders WHERE enabled = 1 AND fire_at < ?", (to_iso(moment),)
        )
        return [Reminder.from_row(row) for row in rows]

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM reminders", default=0) or 0)

    # ------------------------------------------------------------------ log
    def log_delivery(
        self,
        reminder_id: int,
        *,
        fire_at: datetime,
        shown_at: datetime,
        late_by_s: int,
        delivered: bool = True,
        note: str = "",
    ) -> None:
        self.db.execute_write(
            """
            INSERT INTO reminder_log(reminder_id, fire_at, shown_at, was_late, late_by_s, delivered, note)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(reminder_id),
                to_iso(fire_at),
                to_iso(shown_at),
                1 if late_by_s > LATE_GRACE_SECONDS else 0,
                int(late_by_s),
                int(delivered),
                note[:300],
            ),
        )

    def history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT l.*, r.title AS title FROM reminder_log l
            LEFT JOIN reminders r ON r.id = l.reminder_id
            ORDER BY l.id DESC LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(row) for row in rows]


class ReminderService:
    """High-level reminder operations used by tools and the UI."""

    def __init__(self, repository: ReminderRepository) -> None:
        self.repo = repository

    def create(
        self,
        title: str,
        fire_at: datetime,
        *,
        body: str = "",
        recurrence: str = "none",
        task_id: int | None = None,
    ) -> Reminder:
        if fire_at <= now() and recurrence == "none":
            # Do not silently back-date: push to the near future instead.
            fire_at = now() + timedelta(minutes=1)
        return self.repo.create(title, fire_at, body=body, recurrence=recurrence, task_id=task_id)

    def cancel(self, reminder_id: int) -> bool:
        return self.repo.delete(reminder_id)

    def snooze(self, reminder_id: int, *, minutes: int = 10) -> Optional[Reminder]:
        reminder = self.repo.get(reminder_id)
        if reminder is None:
            return None
        base = reminder.fire_at or now()
        target = max(now(), base) + timedelta(minutes=max(1, minutes))
        return self.repo.update(reminder_id, fire_at=target)

    def next_occurrence_after(self, reminder: Reminder, moment: datetime) -> Optional[datetime]:
        if not reminder.repeats:
            return None
        anchor = reminder.fire_at
        return recurrence_rules.next_from_now(reminder.recurrence, moment, anchor=anchor)

    def upcoming_text(self, *, limit: int = 10) -> str:
        items = self.repo.upcoming(limit=limit)
        if not items:
            return "No reminders scheduled."
        lines = []
        for reminder in items:
            repeat = f" ({recurrence_rules.describe(reminder.recurrence)})" if reminder.repeats else ""
            lines.append(f"#{reminder.id} {reminder.title} - {humanise_due(reminder.fire_at)}{repeat}")
        return "\n".join(lines)


class ReminderScheduler:
    """Background ticker that delivers reminders on time (or explains why not)."""

    def __init__(
        self,
        repository: ReminderRepository,
        *,
        deliver: Callable[[ReminderDelivery], None],
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        catch_up: bool = True,
    ) -> None:
        self.repo = repository
        self.deliver = deliver
        self.tick_seconds = max(2.0, float(tick_seconds))
        self.catch_up = catch_up
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_tick: Optional[datetime] = None
        self._started_at: Optional[datetime] = None
        self._delivered_count = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = now()
        self._thread = threading.Thread(target=self._run, name="reminder-scheduler", daemon=True)
        self._thread.start()
        log.info("reminder scheduler started (tick=%ss)", self.tick_seconds)

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        log.info("reminder scheduler stopped")

    def wake(self) -> None:
        """Ask the scheduler to tick immediately (after adding a reminder)."""
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "tick_seconds": self.tick_seconds,
            "last_tick": to_iso(self._last_tick),
            "started_at": to_iso(self._started_at),
            "delivered": self._delivered_count,
        }

    # ----------------------------------------------------------------- loop
    def _run(self) -> None:
        # Deliver anything that came due while LUMI was closed.
        if self.catch_up:
            try:
                self._recover_missed()
            except Exception:  # pragma: no cover - recovery must not stop the loop
                log.exception("missed-reminder recovery failed")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - a bad tick must not kill the loop
                log.exception("reminder tick failed")
            self._wake.wait(self.tick_seconds)
            self._wake.clear()

    def _recover_missed(self) -> None:
        """Fire reminders that were due while the app was not running."""
        reference = now()
        missed = self.repo.due(reference=reference, limit=25)
        if not missed:
            return
        log.info("recovering %s reminder(s) that were due while LUMI was closed", len(missed))
        for reminder in missed[:MAX_CATCHUP_PER_TICK]:
            self._deliver(reminder, scheduled=reminder.fire_at or reference, reference=reference, recovered=True)
        leftover = missed[MAX_CATCHUP_PER_TICK:]
        if leftover:
            # Push the remainder into the next tick rather than flooding the user.
            log.info("%s further reminders will be delivered on the next tick", len(leftover))

    def tick(self, *, reference: datetime | None = None) -> list[ReminderDelivery]:
        """Deliver everything that is due right now.  Safe to call directly in tests."""
        ref = reference or now()
        self._last_tick = ref
        deliveries: list[ReminderDelivery] = []
        for reminder in self.repo.due(reference=ref, limit=MAX_CATCHUP_PER_TICK):
            delivery = self._deliver(reminder, scheduled=reminder.fire_at or ref, reference=ref, recovered=False)
            if delivery is not None:
                deliveries.append(delivery)
        return deliveries

    def _deliver(
        self,
        reminder: Reminder,
        *,
        scheduled: datetime,
        reference: datetime,
        recovered: bool,
    ) -> Optional[ReminderDelivery]:
        if reminder.id is None:
            return None
        late_by = max(0, int((reference - scheduled).total_seconds()))
        message = reminder.body.strip() or reminder.title
        if late_by > LATE_GRACE_SECONDS:
            if recovered:
                reason = "LUMI was not running"
            else:
                reason = "your computer was probably asleep or busy"
            message = f"{message}\n\n(This was due {humanise_delta(scheduled, reference=reference)} - {reason}.)"

        delivery = ReminderDelivery(
            reminder=reminder,
            scheduled_for=scheduled,
            delivered_at=reference,
            late_by_seconds=late_by,
            message=message,
        )

        try:
            self.deliver(delivery)
            delivered = True
        except Exception as exc:  # pragma: no cover - notification failure must not lose the reminder
            log.warning("could not deliver reminder %s: %s", reminder.id, exc)
            delivered = False

        with self._lock:
            self._delivered_count += 1

        try:
            self.repo.log_delivery(
                reminder.id,
                fire_at=scheduled,
                shown_at=reference,
                late_by_s=late_by,
                delivered=delivered,
                note="recovered after restart" if recovered else "",
            )
            nxt = self._advance(reminder, reference)
            # A reminder with no next occurrence is finished: disabling it is what
            # stops a one-off reminder from firing again on the very next tick.
            self.repo.update(
                reminder.id,
                last_fired_at=reference,
                fire_at=nxt if nxt is not None else reminder.fire_at,
                enabled=nxt is not None,
            )
        except Exception:  # pragma: no cover - storage problems are logged, not fatal
            log.exception("could not update reminder %s after delivery", reminder.id)

        return delivery

    def _advance(self, reminder: Reminder, reference: datetime) -> Optional[datetime]:
        """Compute the next fire time for a repeating reminder."""
        if not reminder.repeats:
            return None
        anchor = reminder.fire_at or reference
        nxt = recurrence_rules.next_from_now(reminder.recurrence, reference, anchor=anchor)
        if nxt is None:
            return None
        return nxt


def format_reminder_line(reminder: Reminder) -> str:
    repeat = f" · {recurrence_rules.describe(reminder.recurrence)}" if reminder.repeats else ""
    state = "" if reminder.enabled else " · paused"
    return f"#{reminder.id} {reminder.title} - {humanise_due(reminder.fire_at)}{repeat}{state}"
