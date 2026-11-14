package com.lumi.core.model

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId

enum class Priority { LOW, NORMAL, HIGH }

data class Subtask(val title: String, val done: Boolean = false)

/** A unit of work the user wants to get done. Reminders are attached to tasks, never free-floating. */
data class Task(
    val id: Long = 0,
    val title: String,
    val description: String = "",
    val createdAt: Long,
    val dueAt: Long? = null,
    val priority: Priority = Priority.NORMAL,
    val completed: Boolean = false,
    val completedAt: Long? = null,
    val category: String? = null,
    val subtasks: List<Subtask> = emptyList(),
    /** Encoded [com.lumi.core.time.Recurrence]; non-null marks a recurring task. */
    val recurrence: String? = null,
    val notes: String = "",
    /** For recurring tasks: when an occurrence was last marked done. */
    val lastCompletedAt: Long? = null,
)

enum class ReminderStatus { SCHEDULED, FIRED, MISSED, DONE, CANCELLED }

enum class PlaceTransition { ENTER, EXIT }

/** One delivery schedule for a task: either a point in time (optionally recurring) or a place trigger. */
data class Reminder(
    val id: Long = 0,
    val taskId: Long,
    val triggerAt: Long? = null,
    /** First occurrence of a recurring series; recurrence maths is always relative to this. */
    val anchorAt: Long? = null,
    val recurrence: String? = null,
    val placeName: String? = null,
    val transition: PlaceTransition? = null,
    val status: ReminderStatus = ReminderStatus.SCHEDULED,
    /** When > 0, re-notify every this many minutes until the task is completed. */
    val nagMinutes: Int = 0,
    val nagCount: Int = 0,
    val lastFiredAt: Long? = null,
    /** False when the OS only allowed an inexact alarm. */
    val exact: Boolean = true,
) {
    val isLocationBased: Boolean get() = placeName != null
}

data class Place(
    val id: Long = 0,
    val name: String,
    val latitude: Double,
    val longitude: Double,
    val radiusMeters: Float = 150f,
)

enum class MemoryCategory(val label: String) {
    EXPLICIT("Things you asked me to remember"),
    PREFERENCE("Preferences"),
    ROUTINE("Routines and schedule"),
    CONTACT_ALIAS("Contact nicknames"),
    APP_PREFERENCE("App preferences"),
    HISTORY("Context from conversations"),
    PATTERN("Approved recurring patterns"),
    NOTE("Notes"),
}

data class Memory(
    val id: Long = 0,
    val category: MemoryCategory,
    /** Stable lookup key for single-valued facts (e.g. "preferred_name"); null for free-form memories. */
    val key: String? = null,
    val content: String,
    /** "explicit", "onboarding" or "learned". */
    val source: String = "explicit",
    val createdAt: Long,
    val updatedAt: Long,
)

enum class PatternStatus { OBSERVING, OFFERED, APPROVED, DECLINED }

data class PatternRecord(
    val key: String,
    val label: String,
    val count: Int,
    val status: PatternStatus,
    val lastSeenAt: Long,
)

data class Turn(val role: String, val text: String, val at: Long)

/** Wall-clock access, injectable so scheduling logic can be tested deterministically. */
interface TimeSource {
    fun nowMillis(): Long
    fun zone(): ZoneId
}

fun TimeSource.now(): LocalDateTime = toLocal(nowMillis())
fun TimeSource.today(): LocalDate = now().toLocalDate()
fun TimeSource.toLocal(millis: Long): LocalDateTime = LocalDateTime.ofInstant(Instant.ofEpochMilli(millis), zone())
fun TimeSource.toMillis(local: LocalDateTime): Long = local.atZone(zone()).toInstant().toEpochMilli()

object SystemTimeSource : TimeSource {
    override fun nowMillis(): Long = System.currentTimeMillis()
    override fun zone(): ZoneId = ZoneId.systemDefault()
}

object SubtaskCodec {
    fun encode(items: List<Subtask>): String =
        items.joinToString("\n") { (if (it.done) "1" else "0") + "\t" + it.title.replace('\n', ' ').replace('\t', ' ') }

    fun decode(text: String?): List<Subtask> {
        if (text.isNullOrBlank()) return emptyList()
        return text.split('\n').mapNotNull { line ->
            val i = line.indexOf('\t')
            if (i < 0) null else Subtask(line.substring(i + 1), line.substring(0, i) == "1")
        }
    }
}
