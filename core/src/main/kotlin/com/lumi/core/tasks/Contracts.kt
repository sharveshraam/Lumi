package com.lumi.core.tasks

import com.lumi.core.model.Place
import com.lumi.core.model.Reminder
import com.lumi.core.model.Task

/** Persistence contract for tasks and reminders (implemented with Room in the app). */
interface TaskRepository {
    suspend fun insertTask(task: Task): Long
    suspend fun updateTask(task: Task)
    /** Deletes the task and all of its reminders. */
    suspend fun deleteTask(id: Long)
    suspend fun getTask(id: Long): Task?
    suspend fun allTasks(): List<Task>

    suspend fun insertReminder(reminder: Reminder): Long
    suspend fun updateReminder(reminder: Reminder)
    suspend fun deleteReminder(id: Long)
    suspend fun getReminder(id: Long): Reminder?
    suspend fun remindersForTask(taskId: Long): List<Reminder>
    suspend fun allReminders(): List<Reminder>
}

/** What actually happened when a reminder was handed to the operating system. */
sealed class ScheduleOutcome {
    /** An exact alarm was registered. */
    object Exact : ScheduleOutcome()
    /** Only an inexact alarm could be registered; delivery may be delayed by the OS. */
    data class Inexact(val reason: String) : ScheduleOutcome()
    /** A geofence was registered for a place trigger. */
    object LocationArmed : ScheduleOutcome()
    /** The place trigger is stored but cannot fire automatically yet. */
    data class LocationPending(val reason: String) : ScheduleOutcome()
    /** Nothing was scheduled. */
    data class Failed(val reason: String) : ScheduleOutcome()

    val delivered: Boolean get() = this is Exact || this is Inexact || this is LocationArmed
}

/** OS scheduling contract (AlarmManager + geofencing in the app). */
interface ReminderScheduler {
    fun scheduleTime(reminderId: Long, triggerAtMillis: Long): ScheduleOutcome
    fun cancelTime(reminderId: Long)
    suspend fun scheduleLocation(reminder: Reminder, place: Place): ScheduleOutcome
    suspend fun cancelLocation(reminderId: Long)
}

interface PlaceStore {
    suspend fun find(name: String): Place?
    suspend fun all(): List<Place>
    /** Inserts, or replaces the place with the same (case-insensitive) name. */
    suspend fun save(place: Place): Long
    suspend fun delete(id: Long)
}
