package com.lumi.core

import com.lumi.core.model.Place
import com.lumi.core.model.PlaceTransition
import com.lumi.core.model.Priority
import com.lumi.core.model.ReminderStatus
import com.lumi.core.tasks.CreateRequest
import com.lumi.core.tasks.ScheduleOutcome
import com.lumi.core.tasks.TaskMatch
import com.lumi.core.tasks.TaskService
import com.lumi.core.time.Freq
import com.lumi.core.time.Recurrence
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDateTime

class TaskServiceTest {
    private val time = FakeTime()
    private val repo = InMemoryTaskRepo()
    private val scheduler = FakeScheduler()
    private val places = InMemoryPlaces()
    private val service = TaskService(repo, scheduler, places, time)

    private val tomorrow9 = time.millis(LocalDateTime.of(2026, 11, 14, 9, 0))

    @Test fun creatingATaskWithATimeCreatesALinkedReminderAndAlarm(): Unit = runBlocking {
        val r = service.create(CreateRequest("Complete my assignment", dueAt = tomorrow9, remindAt = tomorrow9))
        assertEquals(ScheduleOutcome.Exact, r.outcome)
        val reminder = r.reminder!!
        assertEquals(r.task.id, reminder.taskId)
        assertEquals(tomorrow9, reminder.triggerAt)
        assertEquals(tomorrow9, scheduler.alarms[reminder.id])
        assertEquals(tomorrow9, repo.getTask(r.task.id)!!.dueAt)
    }

    @Test fun aTaskWithoutATimeHasNoReminder(): Unit = runBlocking {
        val r = service.create(CreateRequest("Clean the desk"))
        assertNull(r.reminder)
        assertNull(r.outcome)
        assertTrue(scheduler.alarms.isEmpty())
        assertEquals(1, service.pending().size)
    }

    @Test fun schedulingFailureIsReportedAndNothingIsLeftArmed(): Unit = runBlocking {
        scheduler.timeOutcome = ScheduleOutcome.Failed("alarms are blocked")
        val r = service.create(CreateRequest("Pay rent", dueAt = tomorrow9, remindAt = tomorrow9))
        assertTrue(r.outcome is ScheduleOutcome.Failed)
        assertTrue(scheduler.alarms.isEmpty())
        // The task itself is still kept so the user does not lose it.
        assertEquals(1, service.pending().size)
    }

    @Test fun inexactFallbackIsRecorded(): Unit = runBlocking {
        scheduler.timeOutcome = ScheduleOutcome.Inexact("exact alarms not allowed")
        val r = service.create(CreateRequest("Pay rent", dueAt = tomorrow9, remindAt = tomorrow9))
        assertTrue(r.outcome is ScheduleOutcome.Inexact)
        assertFalse(repo.getReminder(r.reminder!!.id)!!.exact)
        assertEquals(tomorrow9, scheduler.alarms[r.reminder!!.id])
    }

    @Test fun severalRemindersForOneTask(): Unit = runBlocking {
        val r = service.create(CreateRequest("Exam", dueAt = tomorrow9, remindAt = tomorrow9))
        val (second, outcome) = service.addReminder(r.task.id, tomorrow9 - 3_600_000)
        assertEquals(ScheduleOutcome.Exact, outcome)
        assertEquals(2, service.remindersFor(r.task.id).size)
        assertEquals(2, scheduler.alarms.size)
        assertTrue(service.cancelReminder(second.id))
        assertEquals(1, scheduler.alarms.size)
    }

    @Test fun firingAOneTimeReminderMarksItFired(): Unit = runBlocking {
        val r = service.create(CreateRequest("Call mom", dueAt = tomorrow9, remindAt = tomorrow9))
        time.current = tomorrow9
        val fired = service.onAlarm(r.reminder!!.id)!!
        assertEquals("Call mom", fired.task.title)
        assertFalse(fired.missed)
        assertEquals(ReminderStatus.FIRED, repo.getReminder(r.reminder!!.id)!!.status)
    }

    @Test fun alarmsForCompletedOrDeletedTasksAreIgnored(): Unit = runBlocking {
        val r = service.create(CreateRequest("Call mom", dueAt = tomorrow9, remindAt = tomorrow9))
        service.complete(r.task.id)
        assertTrue(scheduler.alarms.isEmpty())
        assertNull(service.onAlarm(r.reminder!!.id))
        assertNull(service.onAlarm(9999))
    }

    @Test fun recurringReminderSchedulesItsNextOccurrenceWhenItFires(): Unit = runBlocking {
        val r = service.create(CreateRequest("Take vitamins", dueAt = tomorrow9, remindAt = tomorrow9, recurrence = Recurrence(Freq.DAILY)))
        time.current = tomorrow9
        assertNotNull(service.onAlarm(r.reminder!!.id))
        val next = time.millis(LocalDateTime.of(2026, 11, 15, 9, 0))
        assertEquals(next, scheduler.alarms[r.reminder!!.id])
        assertEquals(ReminderStatus.SCHEDULED, repo.getReminder(r.reminder!!.id)!!.status)
    }

    @Test fun completingARecurringTaskRollsItForwardInsteadOfClosingIt(): Unit = runBlocking {
        val r = service.create(CreateRequest("Take vitamins", dueAt = tomorrow9, remindAt = tomorrow9, recurrence = Recurrence(Freq.DAILY)))
        time.current = tomorrow9 + 60_000
        val task = service.complete(r.task.id)!!
        assertFalse(task.completed)
        assertEquals(time.millis(LocalDateTime.of(2026, 11, 15, 9, 0)), task.dueAt)
        assertNotNull(task.lastCompletedAt)
        assertEquals(1, service.recurring().size)
    }

    @Test fun snoozeMovesTheAlarm(): Unit = runBlocking {
        val r = service.create(CreateRequest("Stretch", dueAt = tomorrow9, remindAt = tomorrow9))
        time.current = tomorrow9
        service.onAlarm(r.reminder!!.id)
        val result = service.snooze(r.reminder!!.id, 10)!!
        assertEquals(tomorrow9 + 600_000, result.reminder.triggerAt)
        assertEquals(tomorrow9 + 600_000, scheduler.alarms[r.reminder!!.id])
        assertEquals(ReminderStatus.SCHEDULED, repo.getReminder(r.reminder!!.id)!!.status)
    }

    @Test fun postponeByAnHourMovesReminderAndDueDate(): Unit = runBlocking {
        val r = service.create(CreateRequest("Assignment", dueAt = tomorrow9, remindAt = tomorrow9))
        val result = service.postpone(r.task.id, byMinutes = 60)!!
        assertEquals(tomorrow9 + 3_600_000, result.reminder.triggerAt)
        assertEquals(tomorrow9 + 3_600_000, repo.getTask(r.task.id)!!.dueAt)
        assertEquals(tomorrow9 + 3_600_000, scheduler.alarms[r.reminder!!.id])
    }

    @Test fun postponingAnOverdueTaskCountsFromNow(): Unit = runBlocking {
        val r = service.create(CreateRequest("Assignment", dueAt = tomorrow9, remindAt = tomorrow9))
        time.current = tomorrow9
        service.onAlarm(r.reminder!!.id)
        time.current = tomorrow9 + 5 * 3_600_000
        val result = service.postpone(r.task.id, byMinutes = 30)!!
        assertEquals(time.current + 30 * 60_000, result.reminder.triggerAt)
    }

    @Test fun postponingATaskThatNeverHadAReminderCreatesOne(): Unit = runBlocking {
        val r = service.create(CreateRequest("Clean the desk"))
        val target = time.millis(LocalDateTime.of(2026, 11, 13, 18, 0))
        val result = service.postpone(r.task.id, toMillis = target)!!
        assertEquals(target, scheduler.alarms[result.reminder.id])
    }

    @Test fun remindUntilDoneRepeatsAndStopsOnCompletion(): Unit = runBlocking {
        val r = service.create(CreateRequest("Submit form", dueAt = tomorrow9, remindAt = tomorrow9))
        service.setNag(r.task.id, 10)
        time.current = tomorrow9
        val first = service.onAlarm(r.reminder!!.id)!!
        assertFalse(first.nag)
        assertEquals(tomorrow9 + 600_000, scheduler.alarms[r.reminder!!.id])
        time.current = tomorrow9 + 600_000
        val second = service.onAlarm(r.reminder!!.id)!!
        assertTrue(second.nag)
        service.complete(r.task.id)
        assertTrue(scheduler.alarms.isEmpty())
        assertTrue(repo.getTask(r.task.id)!!.completed)
    }

    @Test fun remindUntilDoneGivesUpAfterTheCap(): Unit = runBlocking {
        val r = service.create(CreateRequest("Submit form", dueAt = tomorrow9, remindAt = tomorrow9))
        service.setNag(r.task.id, 10)
        time.current = tomorrow9
        var fires = 0
        while (scheduler.alarms.containsKey(r.reminder!!.id) && fires < 50) {
            time.current = scheduler.alarms[r.reminder!!.id]!!
            scheduler.alarms.remove(r.reminder!!.id)
            service.onAlarm(r.reminder!!.id)
            fires++
        }
        assertEquals(1 + TaskService.MAX_NAGS, fires)
    }

    @Test fun restoreAfterRebootReRegistersFutureAlarms(): Unit = runBlocking {
        val a = service.create(CreateRequest("A", dueAt = tomorrow9, remindAt = tomorrow9))
        val b = service.create(CreateRequest("B", dueAt = tomorrow9 + 1000, remindAt = tomorrow9 + 1000))
        scheduler.alarms.clear() // a reboot wipes AlarmManager
        val report = service.restore()
        assertEquals(2, report.rescheduled)
        assertEquals(0, report.failed)
        assertTrue(report.missed.isEmpty())
        assertEquals(tomorrow9, scheduler.alarms[a.reminder!!.id])
        assertEquals(tomorrow9 + 1000, scheduler.alarms[b.reminder!!.id])
    }

    @Test fun restoreReportsRemindersMissedWhileThePhoneWasOff(): Unit = runBlocking {
        val a = service.create(CreateRequest("A", dueAt = tomorrow9, remindAt = tomorrow9))
        scheduler.alarms.clear()
        time.current = tomorrow9 + 2 * 3_600_000
        val report = service.restore()
        assertEquals(1, report.missed.size)
        assertTrue(report.missed[0].missed)
        assertEquals(ReminderStatus.MISSED, repo.getReminder(a.reminder!!.id)!!.status)
        // Reported once, not on every restore.
        assertTrue(service.restore().missed.isEmpty())
        assertEquals(listOf("A"), service.overdue().map { it.title })
    }

    @Test fun restoreMovesMissedRecurringRemindersToTheirNextOccurrence(): Unit = runBlocking {
        val a = service.create(CreateRequest("Vitamins", dueAt = tomorrow9, remindAt = tomorrow9, recurrence = Recurrence(Freq.DAILY)))
        scheduler.alarms.clear()
        time.set(LocalDateTime.of(2026, 11, 17, 12, 0))
        val report = service.restore()
        assertEquals(1, report.missed.size)
        assertEquals(time.millis(LocalDateTime.of(2026, 11, 18, 9, 0)), scheduler.alarms[a.reminder!!.id])
    }

    @Test fun restoreCountsFailures(): Unit = runBlocking {
        service.create(CreateRequest("A", dueAt = tomorrow9, remindAt = tomorrow9))
        scheduler.alarms.clear()
        scheduler.timeOutcome = ScheduleOutcome.Failed("no permission")
        val report = service.restore()
        assertEquals(0, report.rescheduled)
        assertEquals(1, report.failed)
    }

    @Test fun locationReminderArmsWhenThePlaceIsKnown(): Unit = runBlocking {
        places.save(Place(name = "Home", latitude = 8.5, longitude = 76.9))
        val r = service.create(CreateRequest("Buy milk", placeName = "home", transition = PlaceTransition.ENTER))
        assertEquals(ScheduleOutcome.LocationArmed, r.outcome)
        assertTrue(scheduler.geofences.containsKey(r.reminder!!.id))
        val fired = service.onPlaceTriggered(r.reminder!!.id)!!
        assertEquals("Buy milk", fired.task.title)
        assertFalse(scheduler.geofences.containsKey(r.reminder!!.id))
    }

    @Test fun locationReminderForAnUnknownPlaceStaysPendingUntilThePlaceIsSaved(): Unit = runBlocking {
        val r = service.create(CreateRequest("Buy milk", placeName = "college", transition = PlaceTransition.ENTER))
        assertTrue(r.outcome is ScheduleOutcome.LocationPending)
        assertTrue(scheduler.geofences.isEmpty())
        val place = Place(name = "College", latitude = 8.5, longitude = 76.9)
        places.save(place)
        assertEquals(1, service.armRemindersForPlace(places.find("college")!!))
        assertTrue(scheduler.geofences.containsKey(r.reminder!!.id))
        assertEquals(1, service.locationReminders().size)
    }

    @Test fun views(): Unit = runBlocking {
        val today6pm = time.millis(LocalDateTime.of(2026, 11, 13, 18, 0))
        val yesterday = time.millis(LocalDateTime.of(2026, 11, 12, 18, 0))
        service.create(CreateRequest("Today thing", dueAt = today6pm, remindAt = today6pm))
        service.create(CreateRequest("Late thing", dueAt = yesterday))
        service.create(CreateRequest("Big thing", dueAt = tomorrow9, remindAt = tomorrow9, priority = Priority.HIGH))
        val done = service.create(CreateRequest("Done thing"))
        service.complete(done.task.id)

        assertEquals(3, service.pending().size)
        assertEquals(listOf("Done thing"), service.completed().map { it.title })
        assertEquals(listOf("Late thing"), service.overdue().map { it.title })
        assertEquals(listOf("Big thing"), service.important().map { it.title })
        assertEquals(listOf("Today thing"), service.dueOn(LocalDateTime.of(2026, 11, 13, 0, 0).toLocalDate()).map { it.title })
        assertEquals(setOf("Today thing", "Big thing"), service.upcoming().map { it.title }.toSet())
        assertEquals(1, service.deleteCompleted())
        assertTrue(service.completed().isEmpty())
    }

    @Test fun reopenAndDelete(): Unit = runBlocking {
        val r = service.create(CreateRequest("Thing", dueAt = tomorrow9, remindAt = tomorrow9))
        service.complete(r.task.id)
        assertFalse(service.reopen(r.task.id)!!.completed)
        assertTrue(service.delete(r.task.id))
        assertTrue(scheduler.alarms.isEmpty())
        assertTrue(repo.allReminders().isEmpty())
        assertFalse(service.delete(r.task.id))
    }

    @Test fun fuzzyFind(): Unit = runBlocking {
        service.create(CreateRequest("Complete my physics assignment"))
        service.create(CreateRequest("Buy milk"))
        service.create(CreateRequest("Buy bread"))
        assertEquals("Complete my physics assignment", (service.find("assignment") as TaskMatch.One).task.title)
        assertEquals("Buy milk", (service.find("the milk") as TaskMatch.One).task.title)
        assertTrue(service.find("buy") is TaskMatch.Many)
        assertTrue(service.find("dentist") is TaskMatch.None)
    }
}
