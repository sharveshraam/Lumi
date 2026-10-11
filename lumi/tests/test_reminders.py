"""Reminders: scheduling, recovery after a restart, repeats and late delivery."""

from __future__ import annotations

from datetime import datetime, timedelta

from lumi.reminders.service import (
    LATE_GRACE_SECONDS,
    ReminderDelivery,
    ReminderRepository,
    ReminderScheduler,
    ReminderService,
)


def test_create_and_list(reminders: ReminderService, reminder_repo: ReminderRepository) -> None:
    fire_at = datetime.now() + timedelta(hours=2)
    reminder = reminders.create("Call mom", fire_at, body="Say hello")
    assert reminder.id is not None
    # Seconds are preserved so short reminders ('in 90 seconds') stay accurate.
    assert reminder.fire_at == fire_at.replace(microsecond=0)
    assert [item.title for item in reminder_repo.upcoming()] == ["Call mom"]


def test_past_reminder_is_pushed_forward(reminders: ReminderService) -> None:
    reminder = reminders.create("Old reminder", datetime.now() - timedelta(days=1))
    assert reminder.fire_at > datetime.now()


def test_snooze_moves_the_time(reminders: ReminderService) -> None:
    reminder = reminders.create("Drink water", datetime.now() + timedelta(minutes=5))
    snoozed = reminders.snooze(int(reminder.id), minutes=15)
    assert snoozed is not None
    assert snoozed.fire_at >= datetime.now() + timedelta(minutes=14)


def test_cancel_removes_it(reminders: ReminderService, reminder_repo: ReminderRepository) -> None:
    reminder = reminders.create("Temporary", datetime.now() + timedelta(minutes=10))
    assert reminders.cancel(int(reminder.id)) is True
    assert reminder_repo.count() == 0


def test_scheduler_delivers_due_reminder(reminder_repo: ReminderRepository) -> None:
    delivered: list[ReminderDelivery] = []
    scheduler = ReminderScheduler(reminder_repo, deliver=delivered.append, tick_seconds=60, catch_up=False)
    reminder = reminder_repo.create("Submit form", datetime.now() - timedelta(seconds=5))
    results = scheduler.tick()
    assert len(results) == 1
    assert delivered[0].reminder.id == reminder.id
    assert delivered[0].was_late is False
    # A one-off reminder is disabled after firing, not deleted.
    refreshed = reminder_repo.get(int(reminder.id))
    assert refreshed is not None and refreshed.last_fired_at is not None


def test_scheduler_advances_recurring_reminder(reminder_repo: ReminderRepository) -> None:
    scheduler = ReminderScheduler(reminder_repo, deliver=lambda delivery: None, tick_seconds=60, catch_up=False)
    reminder = reminder_repo.create("Daily standup", datetime.now() - timedelta(minutes=1), recurrence="daily")
    scheduler.tick()
    refreshed = reminder_repo.get(int(reminder.id))
    assert refreshed is not None
    assert refreshed.enabled is True
    assert refreshed.fire_at > datetime.now()
    assert refreshed.fire_at.hour == reminder.fire_at.hour


def test_late_reminder_explains_itself(reminder_repo: ReminderRepository) -> None:
    delivered: list[ReminderDelivery] = []
    scheduler = ReminderScheduler(reminder_repo, deliver=delivered.append, tick_seconds=60, catch_up=False)
    reminder = reminder_repo.create("Missed appointment", datetime.now() - timedelta(hours=3))
    scheduler.tick()
    assert delivered[0].was_late
    assert "computer was probably asleep" in delivered[0].message


def test_recovery_after_restart_reports_the_reason(reminder_repo: ReminderRepository) -> None:
    delivered: list[ReminderDelivery] = []
    scheduler = ReminderScheduler(reminder_repo, deliver=delivered.append, tick_seconds=60, catch_up=True)
    reminder_repo.create("Was due while closed", datetime.now() - timedelta(hours=20))
    scheduler._recover_missed()
    assert len(delivered) == 1
    assert "LUMI was not running" in delivered[0].message
    assert delivered[0].late_by_seconds > LATE_GRACE_SECONDS


def test_delivery_log_records_history(reminder_repo: ReminderRepository) -> None:
    scheduler = ReminderScheduler(reminder_repo, deliver=lambda delivery: None, tick_seconds=60, catch_up=False)
    reminder_repo.create("Logged one", datetime.now() - timedelta(seconds=1))
    scheduler.tick()
    history = reminder_repo.history()
    assert len(history) == 1
    assert history[0]["title"] == "Logged one"
    assert history[0]["delivered"] == 1


def test_failure_to_notify_is_still_recorded(reminder_repo: ReminderRepository) -> None:
    def exploding_delivery(delivery: ReminderDelivery) -> None:
        raise RuntimeError("no notification service")

    scheduler = ReminderScheduler(reminder_repo, deliver=exploding_delivery, tick_seconds=60, catch_up=False)
    reminder_repo.create("Cannot notify", datetime.now() - timedelta(seconds=1))
    results = scheduler.tick()
    assert len(results) == 1, "a failed notification must not lose the reminder"
    assert reminder_repo.history()[0]["delivered"] == 0


def test_scheduler_start_and_stop(reminder_repo: ReminderRepository) -> None:
    scheduler = ReminderScheduler(reminder_repo, deliver=lambda delivery: None, tick_seconds=3600)
    scheduler.start()
    assert scheduler.running
    scheduler.stop()
    assert not scheduler.running
    assert scheduler.stats()["running"] is False


def test_reminder_text_output(reminders: ReminderService) -> None:
    assert reminders.upcoming_text() == "No reminders scheduled."
    reminders.create("Something soon", datetime.now() + timedelta(minutes=30))
    text = reminders.upcoming_text()
    assert "Something soon" in text
