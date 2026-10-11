"""Tasks: creation, persistence, editing, recurrence and statistics."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lumi.memory.store import Database
from lumi.tasks.models import Task
from lumi.tasks.repository import TaskRepository
from lumi.utils.timeutil import now


def test_create_and_get(tasks: TaskRepository) -> None:
    task = tasks.create("Submit lab record", category="college", priority="high", due_at=datetime(2026, 10, 12, 18, 0))
    assert task.id is not None
    assert task.status == "pending"
    assert task.category == "college"
    assert task.priority == "high"

    fetched = tasks.get(int(task.id))
    assert fetched is not None
    assert fetched.title == "Submit lab record"
    assert fetched.due_at == datetime(2026, 10, 12, 18, 0)


def test_creation_rejects_empty_titles(tasks: TaskRepository) -> None:
    with pytest.raises(ValueError):
        tasks.create("   ")
    with pytest.raises(ValueError):
        tasks.create("x" * 400)


def test_update_and_complete(tasks: TaskRepository) -> None:
    task = tasks.create("Read chapter 4")
    updated = tasks.update(int(task.id), title="Read chapter 5", priority="urgent")
    assert updated is not None and updated.title == "Read chapter 5" and updated.priority == "urgent"

    completed = tasks.complete(int(task.id))
    assert completed is not None
    assert completed.status == "done"
    assert completed.completed_at is not None

    reopened = tasks.complete(int(task.id), completed=False)
    assert reopened is not None and reopened.status == "pending" and reopened.completed_at is None


def test_invalid_status_is_rejected(tasks: TaskRepository) -> None:
    task = tasks.create("Something")
    with pytest.raises(ValueError):
        tasks.update(int(task.id), status="finished")


def test_postpone_moves_due_date_and_reminder(tasks: TaskRepository) -> None:
    due = now() + timedelta(days=1)
    task = tasks.create("Assignment", due_at=due, remind_at=due - timedelta(hours=2))
    postponed = tasks.postpone(int(task.id), days=2)
    assert postponed is not None
    assert postponed.due_at == due + timedelta(days=2)
    assert postponed.remind_at == postponed.due_at - timedelta(hours=2)


def test_persistence_across_reopen(tmp_path) -> None:
    path = tmp_path / "lumi.db"
    first = Database(path)
    first.connect()
    repo = TaskRepository(first)
    task = repo.create("Persisted task", due_at=datetime(2026, 11, 1, 9, 0))
    first.close()

    second = Database(path)
    second.connect()
    reopened = TaskRepository(second)
    loaded = reopened.get(int(task.id))
    second.close()

    assert loaded is not None
    assert loaded.title == "Persisted task"
    assert loaded.due_at == datetime(2026, 11, 1, 9, 0)


def test_queries_for_today_overdue_and_upcoming(tasks: TaskRepository) -> None:
    reference = datetime(2026, 10, 11, 12, 0)
    tasks.create("Overdue one", due_at=reference - timedelta(days=1))
    tasks.create("Today one", due_at=reference + timedelta(hours=3))
    tasks.create("Later", due_at=reference + timedelta(days=4))
    tasks.create("No date")
    done = tasks.create("Finished", due_at=reference - timedelta(days=2))
    tasks.complete(int(done.id))

    assert [task.title for task in tasks.overdue(reference=reference)] == ["Overdue one"]
    assert [task.title for task in tasks.due_today(reference=reference)] == ["Today one"]
    # "upcoming" means anything due after the reference time (today included).
    assert [task.title for task in tasks.upcoming(days=7, reference=reference)] == ["Today one", "Later"]
    assert [task.title for task in tasks.without_due_date()] == ["No date"]

    stats = tasks.stats(reference=reference)
    assert stats["open"] == 4
    assert stats["overdue"] == 1
    assert stats["due_today"] == 1
    assert stats["total"] == 5


def test_recurring_task_spawns_next_occurrence(tasks: TaskRepository) -> None:
    task = tasks.create("Daily revision", due_at=now(), recurrence="daily")
    tasks.complete(int(task.id))
    pending = tasks.list(status="open")
    assert len(pending) == 1
    assert pending[0].title == "Daily revision"
    assert pending[0].due_at > task.due_at  # type: ignore[operator]
    assert pending[0].parent_id == task.id


def test_recurrence_end_stops_spawning(tasks: TaskRepository) -> None:
    task = tasks.create(
        "Weekly report",
        due_at=now(),
        recurrence="weekly",
        recurrence_end=now() + timedelta(days=2),
    )
    tasks.complete(int(task.id))
    assert tasks.list(status="open") == []


def test_search_and_link_ambiguous_titles(tasks: TaskRepository) -> None:
    tasks.create("Electronics assignment", category="college")
    tasks.create("Electromagnetics homework")
    assert len(tasks.search("electronics")) == 1
    matches = tasks.link_ambiguous_titles("electro assignment")
    assert matches and matches[0].title == "Electronics assignment"


def test_suggest_order_prefers_overdue_then_deadline(tasks: TaskRepository) -> None:
    reference = datetime(2026, 10, 11, 12, 0)
    tasks.create("Far away", due_at=reference + timedelta(days=6), priority="low")
    tasks.create("Soon urgent", due_at=reference + timedelta(hours=2), priority="urgent")
    tasks.create("Overdue item", due_at=reference - timedelta(hours=4), priority="low")
    order = [task.title for task in tasks.suggest_order(reference=reference)]
    assert order[0] == "Overdue item"
    assert order[1] == "Soon urgent"


def test_delete_completed_only_removes_done_tasks(tasks: TaskRepository) -> None:
    done = tasks.create("Done task")
    tasks.complete(int(done.id))
    tasks.create("Open task")
    removed = tasks.delete_completed()
    assert removed == 1
    assert [task.title for task in tasks.list(status="all")] == ["Open task"]


def test_task_helpers_render_human_text(tasks: TaskRepository) -> None:
    reference = datetime(2026, 10, 11, 12, 0)
    task = tasks.create("Ship it", due_at=reference + timedelta(hours=6))
    assert "Today" in task.human_due(reference)
    assert isinstance(task.sort_key(reference), tuple)
    assert Task.from_row.__doc__ is None  # classmethod exists and is documented by type hints
