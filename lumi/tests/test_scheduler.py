"""Reminder delivery: on time, late, recovered after a restart, and repeating."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lumi.reminders.service import ReminderRepository, ReminderScheduler, ReminderService
from lumi.utils.timeutil import now

REFERENCE = datetime(2026, 10, 11, 14, 30)


@pytest.fixture()
def service(reminder_repo: ReminderRepository) -> ReminderService:
    return ReminderService(reminder_repo)


def collect() -> tuple[list[object], object]:
    delivered: list[object] = []

    def sink(delivery) -> None:
        delivered.append(delivery)

    return delivered, sink


def test_due_reminder_is_delivered(reminder_repo, service) -> None:
    reminder = service.create("stand up", REFERENCE - timedelta(minutes=1))
    delivered, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=False)
    results = scheduler.tick(reference=REFERENCE)
    assert len(results) == 1
    assert delivered[0].reminder.id == reminder.id
    assert delivered[0].was_late is False


def test_future_reminder_is_not_delivered_early(reminder_repo, service) -> None:
    service.create("later", REFERENCE + timedelta(hours=2))
    delivered, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=False)
    assert scheduler.tick(reference=REFERENCE) == []
    assert delivered == []


def test_late_reminder_is_delivered_and_flagged(reminder_repo, service) -> None:
    service.create("stand up", REFERENCE - timedelta(minutes=15))
    delivered, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=False)
    scheduler.tick(reference=REFERENCE)
    assert delivered and delivered[0].was_late is True
    assert delivered[0].late_by_seconds >= 900


def test_disabled_reminder_stays_quiet(reminder_repo, service) -> None:
    reminder = service.create("muted", REFERENCE - timedelta(minutes=1))
    reminder_repo.update(int(reminder.id), enabled=False)
    delivered, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=False)
    assert scheduler.tick(reference=REFERENCE) == []
    assert delivered == []


def test_delivery_is_recorded_in_history(reminder_repo, service) -> None:
    service.create("drink water", REFERENCE - timedelta(seconds=30))
    _, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=False)
    scheduler.tick(reference=REFERENCE)
    history = reminder_repo.history(limit=5)
    assert history and history[0]["title"] == "drink water"


def test_missed_reminders_are_recovered_after_a_restart(reminder_repo, service) -> None:
    """A reminder that came due while LUMI was closed must still reach the user."""
    service.create("submit the form", REFERENCE - timedelta(hours=6))
    delivered, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=True)
    # What the background loop does on its first pass after a restart:
    scheduler.tick(reference=REFERENCE)
    assert len(delivered) == 1
    assert "submit the form" in delivered[0].message
    assert delivered[0].was_late is True


def test_repeating_reminder_advances_instead_of_disappearing(reminder_repo, service) -> None:
    reminder = service.create("drink water", REFERENCE - timedelta(minutes=1), recurrence="daily")
    _, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=False)
    scheduler.tick(reference=REFERENCE)
    refreshed = reminder_repo.get(int(reminder.id))
    assert refreshed is not None
    assert refreshed.enabled is True
    assert refreshed.fire_at > REFERENCE, "a daily reminder must move to the next day"


def test_one_off_reminder_is_disabled_after_firing(reminder_repo, service) -> None:
    reminder = service.create("one off", REFERENCE - timedelta(minutes=1))
    _, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, catch_up=False)
    scheduler.tick(reference=REFERENCE)
    refreshed = reminder_repo.get(int(reminder.id))
    assert refreshed is not None
    assert refreshed.enabled is False


def test_snooze_pushes_the_time_forward(reminder_repo, service) -> None:
    reminder = service.create("snooze me", REFERENCE - timedelta(minutes=1))
    snoozed = service.snooze(int(reminder.id), minutes=10)
    assert snoozed is not None
    assert snoozed.fire_at > now()
    assert snoozed.enabled is True


def test_scheduler_starts_and_stops_cleanly(reminder_repo) -> None:
    _, sink = collect()
    scheduler = ReminderScheduler(reminder_repo, deliver=sink, tick_seconds=0.05, catch_up=False)
    scheduler.start()
    assert scheduler.running is True
    scheduler.stop()
    assert scheduler.running is False
    assert scheduler.stats()["delivered"] >= 0


def test_cancel_stops_a_reminder(reminder_repo, service) -> None:
    reminder = service.create("cancel me", REFERENCE - timedelta(minutes=1))
    assert service.cancel(int(reminder.id)) is True
    delivered, sink = collect()
    ReminderScheduler(reminder_repo, deliver=sink, catch_up=False).tick(reference=REFERENCE)
    assert delivered == []
