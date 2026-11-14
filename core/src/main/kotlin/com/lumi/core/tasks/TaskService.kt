package com.lumi.core.tasks

import com.lumi.core.model.Place
import com.lumi.core.model.PlaceTransition
import com.lumi.core.model.Priority
import com.lumi.core.model.Reminder
import com.lumi.core.model.ReminderStatus
import com.lumi.core.model.Task
import com.lumi.core.model.TimeSource
import com.lumi.core.model.toLocal
import com.lumi.core.model.toMillis
import com.lumi.core.time.Recurrence
import java.time.LocalDate

data class CreateRequest(
    val title: String,
    val description: String = "",
    val dueAt: Long? = null,
    val remindAt: Long? = null,
    val recurrence: Recurrence? = null,
    val priority: Priority = Priority.NORMAL,
    val category: String? = null,
    val placeName: String? = null,
    val transition: PlaceTransition? = null,
    val notes: String = "",
)

data class CreateResult(val task: Task, val reminder: Reminder?, val outcome: ScheduleOutcome?)

data class PostponeResult(val task: Task, val reminder: Reminder, val outcome: ScheduleOutcome)

/** A reminder that should be shown to the user now. */
data class FiredReminder(val task: Task, val reminder: Reminder, val missed: Boolean = false, val nag: Boolean = false)

data class RestoreReport(val rescheduled: Int, val failed: Int, val missed: List<FiredReminder>)

sealed class TaskMatch {
    object None : TaskMatch()
    data class One(val task: Task) : TaskMatch()
    data class Many(val tasks: List<Task>) : TaskMatch()
}

/**
 * All task and reminder rules live here, independent of Android, so they can be unit tested.
 * The service never reports a reminder as scheduled unless the [ReminderScheduler] said so.
 */
class TaskService(
    private val repo: TaskRepository,
    private val scheduler: ReminderScheduler,
    private val places: PlaceStore,
    private val time: TimeSource,
) {
    // ---------------------------------------------------------------- creation

    suspend fun create(req: CreateRequest): CreateResult {
        val now = time.nowMillis()
        val task = Task(
            title = req.title.trim(),
            description = req.description,
            createdAt = now,
            dueAt = req.dueAt ?: req.remindAt,
            priority = req.priority,
            category = req.category?.trim()?.takeIf { it.isNotEmpty() },
            recurrence = req.recurrence?.encode(),
            notes = req.notes,
        )
        val saved = task.copy(id = repo.insertTask(task))
        return when {
            req.placeName != null -> {
                val (reminder, outcome) = addLocationReminder(saved.id, req.placeName, req.transition ?: PlaceTransition.ENTER)
                CreateResult(saved, reminder, outcome)
            }
            req.remindAt != null -> {
                val (reminder, outcome) = addReminder(saved.id, req.remindAt, req.recurrence)
                CreateResult(saved, reminder, outcome)
            }
            else -> CreateResult(saved, null, null)
        }
    }

    /** Adds another time-based reminder to a task. A task can carry any number of reminders. */
    suspend fun addReminder(taskId: Long, at: Long, recurrence: Recurrence? = null): Pair<Reminder, ScheduleOutcome> {
        val draft = Reminder(taskId = taskId, triggerAt = at, anchorAt = at, recurrence = recurrence?.encode())
        val id = repo.insertReminder(draft)
        return arm(draft.copy(id = id))
    }

    suspend fun addLocationReminder(taskId: Long, placeName: String, transition: PlaceTransition): Pair<Reminder, ScheduleOutcome> {
        val draft = Reminder(taskId = taskId, placeName = placeName.trim(), transition = transition)
        val reminder = draft.copy(id = repo.insertReminder(draft))
        return reminder to armLocation(reminder)
    }

    private suspend fun arm(reminder: Reminder): Pair<Reminder, ScheduleOutcome> {
        val at = reminder.triggerAt ?: return reminder to ScheduleOutcome.Failed("This reminder has no time.")
        val outcome = scheduler.scheduleTime(reminder.id, at)
        val updated = reminder.copy(exact = outcome is ScheduleOutcome.Exact, status = ReminderStatus.SCHEDULED)
        repo.updateReminder(updated)
        return updated to outcome
    }

    private suspend fun armLocation(reminder: Reminder): ScheduleOutcome {
        val name = reminder.placeName ?: return ScheduleOutcome.Failed("This reminder has no place.")
        val place = places.find(name)
            ?: return ScheduleOutcome.LocationPending("I don't know where \"$name\" is yet.")
        return scheduler.scheduleLocation(reminder, place)
    }

    /** Arms pending place reminders once a place becomes known. Returns how many were armed. */
    suspend fun armRemindersForPlace(place: Place): Int {
        var armed = 0
        for (r in repo.allReminders()) {
            if (r.status == ReminderStatus.SCHEDULED && r.placeName.equals(place.name, ignoreCase = true)) {
                if (scheduler.scheduleLocation(r, place) is ScheduleOutcome.LocationArmed) armed++
            }
        }
        return armed
    }

    // ---------------------------------------------------------------- state changes

    /** Marks a task done. Recurring tasks roll forward to their next occurrence instead of closing. */
    suspend fun complete(taskId: Long): Task? {
        val task = repo.getTask(taskId) ?: return null
        val now = time.nowMillis()
        val rec = Recurrence.decode(task.recurrence)
        val reminders = repo.remindersForTask(taskId)
        if (rec != null) {
            val anchorMillis = task.dueAt ?: now
            val anchor = time.toLocal(anchorMillis)
            val next = time.toMillis(rec.nextAfter(time.toLocal(maxOf(now, anchorMillis)), anchor))
            val updated = task.copy(dueAt = next, lastCompletedAt = now)
            repo.updateTask(updated)
            for (r in reminders) {
                val trigger = r.triggerAt ?: continue
                val rRec = Recurrence.decode(r.recurrence) ?: continue
                if (r.status == ReminderStatus.CANCELLED || r.status == ReminderStatus.DONE) continue
                // Skip the occurrence that was just completed early, and stop any nagging for it.
                var newTrigger = trigger
                val rAnchor = time.toLocal(r.anchorAt ?: trigger)
                if (trigger < next) newTrigger = time.toMillis(rRec.nextAfter(time.toLocal(next).minusSeconds(1), rAnchor))
                arm(r.copy(triggerAt = newTrigger, nagCount = 0))
            }
            return updated
        }
        val updated = task.copy(completed = true, completedAt = now)
        repo.updateTask(updated)
        for (r in reminders) {
            if (r.status == ReminderStatus.DONE || r.status == ReminderStatus.CANCELLED) continue
            disarm(r)
            repo.updateReminder(r.copy(status = ReminderStatus.DONE))
        }
        return updated
    }

    /** Re-opens a completed task. Its old reminders stay closed; the user can add a new one. */
    suspend fun reopen(taskId: Long): Task? {
        val task = repo.getTask(taskId) ?: return null
        val updated = task.copy(completed = false, completedAt = null)
        repo.updateTask(updated)
        return updated
    }

    suspend fun delete(taskId: Long): Boolean {
        repo.getTask(taskId) ?: return false
        for (r in repo.remindersForTask(taskId)) disarm(r)
        repo.deleteTask(taskId)
        return true
    }

    suspend fun deleteCompleted(): Int {
        val done = repo.allTasks().filter { it.completed }
        for (t in done) delete(t.id)
        return done.size
    }

    suspend fun cancelReminder(reminderId: Long): Boolean {
        val r = repo.getReminder(reminderId) ?: return false
        disarm(r)
        repo.deleteReminder(reminderId)
        return true
    }

    private suspend fun disarm(r: Reminder) {
        if (r.isLocationBased) scheduler.cancelLocation(r.id) else scheduler.cancelTime(r.id)
    }

    /** Saves edits made in the UI. Does not touch reminders. */
    suspend fun update(task: Task) = repo.updateTask(task)

    /** Moves a specific reminder to a new time. */
    suspend fun reschedule(reminderId: Long, newTime: Long, moveDueDate: Boolean = true): PostponeResult? {
        val r = repo.getReminder(reminderId) ?: return null
        val task = repo.getTask(r.taskId) ?: return null
        if (r.isLocationBased) return null
        val (updated, outcome) = arm(r.copy(triggerAt = newTime, anchorAt = if (r.recurrence == null) newTime else r.anchorAt, nagCount = 0))
        var newTask = task
        if (moveDueDate && task.recurrence == null && !task.completed && (task.dueAt == null || task.dueAt == r.triggerAt || task.dueAt < newTime)) {
            newTask = task.copy(dueAt = newTime)
            repo.updateTask(newTask)
        }
        return PostponeResult(newTask, updated, outcome)
    }

    /**
     * Postpones the task's most relevant reminder, either by [byMinutes] or to [toMillis].
     * A reminder that has not fired yet moves relative to its own time; one that already fired
     * moves relative to now. If the task has no time reminder, one is created.
     */
    suspend fun postpone(taskId: Long, byMinutes: Long? = null, toMillis: Long? = null): PostponeResult? {
        val task = repo.getTask(taskId) ?: return null
        val now = time.nowMillis()
        val candidates = repo.remindersForTask(taskId).filter { !it.isLocationBased && it.status != ReminderStatus.CANCELLED }
        val pending = candidates.filter { it.status == ReminderStatus.SCHEDULED && it.triggerAt != null }.minByOrNull { it.triggerAt!! }
        val target = pending ?: candidates.maxByOrNull { it.lastFiredAt ?: 0L }
        val base = maxOf(now, pending?.triggerAt ?: now)
        val newTime = toMillis ?: (base + (byMinutes ?: 60L) * 60_000L)
        if (target == null) {
            val (reminder, outcome) = addReminder(taskId, newTime)
            val newTask = if (task.recurrence == null && (task.dueAt == null || task.dueAt < newTime)) task.copy(dueAt = newTime).also { repo.updateTask(it) } else task
            return PostponeResult(newTask, reminder, outcome)
        }
        return reschedule(target.id, newTime)
    }

    /** Snoozes one reminder from now, without changing the task's due date. */
    suspend fun snooze(reminderId: Long, minutes: Long): PostponeResult? =
        reschedule(reminderId, time.nowMillis() + minutes * 60_000L, moveDueDate = false)

    /** Enables repeat notifications ("remind me again if I haven't completed this"). */
    suspend fun setNag(taskId: Long, everyMinutes: Int): PostponeResult? {
        val task = repo.getTask(taskId) ?: return null
        val now = time.nowMillis()
        val reminders = repo.remindersForTask(taskId).filter { !it.isLocationBased && it.status != ReminderStatus.CANCELLED && it.status != ReminderStatus.DONE }
        val scheduled = reminders.filter { it.status == ReminderStatus.SCHEDULED && it.triggerAt != null }.minByOrNull { it.triggerAt!! }
        if (scheduled != null) {
            val updated = scheduled.copy(nagMinutes = everyMinutes, nagCount = 0)
            repo.updateReminder(updated)
            return PostponeResult(task, updated, if (updated.exact) ScheduleOutcome.Exact else ScheduleOutcome.Inexact("inexact alarm"))
        }
        val fired = reminders.maxByOrNull { it.lastFiredAt ?: 0L }
        val next = now + everyMinutes * 60_000L
        if (fired != null) {
            val (updated, outcome) = arm(fired.copy(triggerAt = next, nagMinutes = everyMinutes, nagCount = 1))
            return PostponeResult(task, updated, outcome)
        }
        val draft = Reminder(taskId = taskId, triggerAt = next, anchorAt = next, nagMinutes = everyMinutes, nagCount = 1)
        val (updated, outcome) = arm(draft.copy(id = repo.insertReminder(draft)))
        return PostponeResult(task, updated, outcome)
    }

    // ---------------------------------------------------------------- delivery

    /**
     * Called when the OS alarm for [reminderId] goes off. Returns what to show, or null if the
     * alarm is stale (task deleted, already completed, or the reminder was moved).
     */
    suspend fun onAlarm(reminderId: Long): FiredReminder? {
        val now = time.nowMillis()
        val r = repo.getReminder(reminderId) ?: return null
        val task = repo.getTask(r.taskId)
        if (task == null) {
            repo.deleteReminder(r.id)
            return null
        }
        val trigger = r.triggerAt
        if (r.status != ReminderStatus.SCHEDULED || trigger == null || trigger > now + STALE_TOLERANCE_MS) return null
        if (task.completed) {
            repo.updateReminder(r.copy(status = ReminderStatus.DONE))
            return null
        }
        return advance(task, r, now, missed = false)
    }

    /** Records delivery and works out the next alarm (recurrence and/or nag). */
    private suspend fun advance(task: Task, r: Reminder, now: Long, missed: Boolean): FiredReminder {
        val wasNag = r.nagCount > 0
        val rec = Recurrence.decode(r.recurrence)
        val nextOccurrence = rec?.let { time.toMillis(it.nextAfter(time.toLocal(now), time.toLocal(r.anchorAt ?: r.triggerAt ?: now))) }
        val nextNag = if (r.nagMinutes > 0 && r.nagCount < MAX_NAGS) now + r.nagMinutes * 60_000L else null
        val updated: Reminder = when {
            nextNag != null && (nextOccurrence == null || nextNag < nextOccurrence) ->
                r.copy(triggerAt = nextNag, nagCount = r.nagCount + 1, lastFiredAt = now, status = ReminderStatus.SCHEDULED)
            nextOccurrence != null ->
                r.copy(triggerAt = nextOccurrence, nagCount = 0, lastFiredAt = now, status = ReminderStatus.SCHEDULED)
            else -> r.copy(lastFiredAt = now, status = if (missed) ReminderStatus.MISSED else ReminderStatus.FIRED)
        }
        val shown = if (updated.status == ReminderStatus.SCHEDULED) arm(updated).first else updated.also { repo.updateReminder(it) }
        return FiredReminder(task, shown, missed = missed, nag = wasNag)
    }

    /** Called when a geofence transition for [reminderId] is reported. */
    suspend fun onPlaceTriggered(reminderId: Long): FiredReminder? {
        val r = repo.getReminder(reminderId) ?: return null
        val task = repo.getTask(r.taskId) ?: return null
        if (r.status != ReminderStatus.SCHEDULED) return null
        scheduler.cancelLocation(r.id)
        if (task.completed) {
            repo.updateReminder(r.copy(status = ReminderStatus.DONE))
            return null
        }
        val updated = r.copy(status = ReminderStatus.FIRED, lastFiredAt = time.nowMillis())
        repo.updateReminder(updated)
        return FiredReminder(task, updated)
    }

    /**
     * Re-registers every pending alarm and geofence with the OS and collects reminders whose time
     * passed while the device was off or the app was restricted. Safe to call repeatedly: it is
     * used after boot, after app updates, on clock changes and by the periodic safety-net worker.
     */
    suspend fun restore(): RestoreReport {
        val now = time.nowMillis()
        var rescheduled = 0
        var failed = 0
        val missed = mutableListOf<FiredReminder>()
        for (r in repo.allReminders()) {
            if (r.status != ReminderStatus.SCHEDULED) continue
            val task = repo.getTask(r.taskId)
            if (task == null) {
                repo.deleteReminder(r.id)
                continue
            }
            if (task.completed) {
                repo.updateReminder(r.copy(status = ReminderStatus.DONE))
                continue
            }
            if (r.isLocationBased) {
                if (armLocation(r) is ScheduleOutcome.LocationArmed) rescheduled++
                continue
            }
            val trigger = r.triggerAt ?: continue
            val grace = if (r.exact) EXACT_GRACE_MS else INEXACT_GRACE_MS
            if (trigger + grace <= now) {
                missed += advance(task, r, now, missed = true)
            } else {
                val (_, outcome) = arm(r)
                if (outcome.delivered) rescheduled++ else failed++
            }
        }
        return RestoreReport(rescheduled, failed, missed)
    }

    // ---------------------------------------------------------------- queries

    suspend fun get(taskId: Long): Task? = repo.getTask(taskId)
    suspend fun remindersFor(taskId: Long): List<Reminder> = repo.remindersForTask(taskId)

    private val ordering = compareBy<Task>({ it.dueAt ?: Long.MAX_VALUE }, { -it.priority.ordinal }, { it.createdAt })

    suspend fun pending(): List<Task> = repo.allTasks().filter { !it.completed }.sortedWith(ordering)
    suspend fun completed(): List<Task> = repo.allTasks().filter { it.completed }.sortedByDescending { it.completedAt ?: 0L }
    suspend fun overdue(): List<Task> = time.nowMillis().let { now -> pending().filter { it.dueAt != null && it.dueAt < now } }
    suspend fun important(): List<Task> = pending().filter { it.priority == Priority.HIGH }
    suspend fun recurring(): List<Task> = pending().filter { it.recurrence != null }
    suspend fun withoutDate(): List<Task> = pending().filter { it.dueAt == null }

    suspend fun upcoming(days: Long = 7): List<Task> {
        val now = time.nowMillis()
        val end = now + days * 86_400_000L
        return pending().filter { it.dueAt != null && it.dueAt in now..end }
    }

    /** Open tasks due on [date], or with a reminder scheduled to fire on it. */
    suspend fun dueOn(date: LocalDate): List<Task> {
        val start = time.toMillis(date.atStartOfDay())
        val end = time.toMillis(date.plusDays(1).atStartOfDay())
        val byReminder = repo.allReminders()
            .filter { it.status == ReminderStatus.SCHEDULED && it.triggerAt != null && it.triggerAt in start until end }
            .map { it.taskId }.toSet()
        return pending().filter { (it.dueAt != null && it.dueAt in start until end) || it.id in byReminder }
    }

    suspend fun dueBetween(startMillis: Long, endMillis: Long): List<Task> =
        pending().filter { it.dueAt != null && it.dueAt in startMillis until endMillis }

    /** Time-based reminders that are still going to fire, soonest first. */
    suspend fun scheduledReminders(): List<Reminder> =
        repo.allReminders().filter { it.status == ReminderStatus.SCHEDULED && it.triggerAt != null }.sortedBy { it.triggerAt }

    suspend fun locationReminders(): List<Pair<Reminder, Task>> =
        repo.allReminders().filter { it.isLocationBased && it.status == ReminderStatus.SCHEDULED }
            .mapNotNull { r -> repo.getTask(r.taskId)?.let { r to it } }

    /** The task whose reminder fired most recently (what "my reminder" usually refers to). */
    suspend fun lastFiredTask(): Task? {
        val r = repo.allReminders().filter { it.lastFiredAt != null }.maxByOrNull { it.lastFiredAt!! } ?: return null
        return repo.getTask(r.taskId)?.takeIf { !it.completed }
    }

    /** Finds tasks by a spoken description such as "my assignment". */
    suspend fun find(query: String, includeCompleted: Boolean = false): TaskMatch {
        val wanted = tokens(query)
        if (wanted.isEmpty()) return TaskMatch.None
        val pool = repo.allTasks().filter { includeCompleted || !it.completed }
        val scored = pool.map { it to score(wanted, tokens(it.title + " " + (it.category ?: ""))) }
            .filter { it.second >= 0.5 }
            .sortedWith(compareByDescending<Pair<Task, Double>> { it.second }.thenBy { it.first.dueAt ?: Long.MAX_VALUE })
        if (scored.isEmpty()) return TaskMatch.None
        val best = scored.first().second
        val top = scored.filter { it.second == best }.map { it.first }
        return if (top.size == 1) TaskMatch.One(top[0]) else TaskMatch.Many(top)
    }

    companion object {
        const val MAX_NAGS = 5
        const val STALE_TOLERANCE_MS = 60_000L
        const val EXACT_GRACE_MS = 2 * 60_000L
        const val INEXACT_GRACE_MS = 20 * 60_000L

        private val STOP = setOf("my", "the", "a", "an", "to", "task", "tasks", "reminder", "reminders", "about", "for", "of", "that", "this", "one", "on", "and")

        internal fun tokens(text: String): List<String> =
            text.lowercase().split(Regex("[^\\p{L}\\p{N}]+")).filter { it.isNotEmpty() && it !in STOP }

        internal fun score(wanted: List<String>, have: List<String>): Double {
            if (wanted.isEmpty() || have.isEmpty()) return 0.0
            val hits = wanted.count { w -> have.any { h -> h == w || (w.length >= 3 && h.startsWith(w)) || (h.length >= 3 && w.startsWith(h)) } }
            val recall = hits.toDouble() / wanted.size
            // Small bonus for tighter matches so "assignment" prefers "Submit assignment" over longer titles.
            return if (hits == 0) 0.0 else recall + 0.01 * hits / have.size
        }
    }
}
