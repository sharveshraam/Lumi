package com.lumi.core.briefing

import com.lumi.core.model.Task
import com.lumi.core.model.TimeSource
import com.lumi.core.model.toLocal
import com.lumi.core.time.TimeFormat
import java.time.LocalDateTime
import java.time.format.TextStyle
import java.util.Locale

/** Which sections the user wants in the daily briefing. */
data class BriefingOptions(
    val tasks: Boolean = true,
    val calendar: Boolean = true,
    val notifications: Boolean = true,
    val battery: Boolean = false,
)

/** Everything the briefing may mention. Sections that could not be read are passed as null. */
data class BriefingData(
    val name: String?,
    val now: LocalDateTime,
    val dueToday: List<Task>,
    val overdue: List<Task>,
    val events: List<String>?,
    val notificationSummary: String?,
    val batteryPercent: Int?,
)

object BriefingComposer {
    fun compose(data: BriefingData, options: BriefingOptions, time: TimeSource): String {
        val sb = StringBuilder()
        val hour = data.now.hour
        val greeting = when {
            hour < 12 -> "Good morning"
            hour < 17 -> "Good afternoon"
            else -> "Good evening"
        }
        sb.append(greeting)
        if (!data.name.isNullOrBlank()) sb.append(", ").append(data.name)
        sb.append(". It's ").append(data.now.dayOfWeek.getDisplayName(TextStyle.FULL, Locale.ENGLISH))
            .append(", ").append(data.now.dayOfMonth).append(' ').append(data.now.month.getDisplayName(TextStyle.FULL, Locale.ENGLISH)).append('.')

        if (options.tasks) {
            if (data.dueToday.isEmpty() && data.overdue.isEmpty()) {
                sb.append(" Nothing is due today.")
            } else {
                if (data.dueToday.isNotEmpty()) {
                    val shown = data.dueToday.take(MAX_ITEMS).joinToString("; ") { t ->
                        val at = t.dueAt?.let { " at " + TimeFormat.time(time.toLocal(it).toLocalTime()) }.orEmpty()
                        t.title + at
                    }
                    val n = data.dueToday.size
                    sb.append(" You have $n ${if (n == 1) "task" else "tasks"} today: $shown")
                    if (n > MAX_ITEMS) sb.append("; and ${n - MAX_ITEMS} more")
                    sb.append('.')
                }
                if (data.overdue.isNotEmpty()) {
                    val n = data.overdue.size
                    sb.append(" $n ${if (n == 1) "task is" else "tasks are"} overdue, including ${data.overdue.first().title}.")
                }
            }
        }
        if (options.calendar && data.events != null) {
            if (data.events.isEmpty()) sb.append(" Your calendar is clear.")
            else {
                sb.append(" On your calendar: ${data.events.take(MAX_ITEMS).joinToString("; ")}")
                if (data.events.size > MAX_ITEMS) sb.append("; and ${data.events.size - MAX_ITEMS} more")
                sb.append('.')
            }
        }
        if (options.notifications && data.notificationSummary != null) sb.append(' ').append(data.notificationSummary)
        if (options.battery && data.batteryPercent != null) sb.append(" Battery is at ${data.batteryPercent} percent.")
        return sb.toString()
    }

    const val MAX_ITEMS = 4
}
