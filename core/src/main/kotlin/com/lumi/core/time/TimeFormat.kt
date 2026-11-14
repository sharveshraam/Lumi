package com.lumi.core.time

import java.time.Duration
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.format.TextStyle
import java.time.temporal.ChronoUnit
import java.util.Locale

/** Speech-friendly date and time phrases. */
object TimeFormat {
    fun time(t: LocalTime): String {
        if (t.hour == 12 && t.minute == 0) return "12 noon"
        if (t.hour == 0 && t.minute == 0) return "midnight"
        val h = if (t.hour % 12 == 0) 12 else t.hour % 12
        val suffix = if (t.hour < 12) "AM" else "PM"
        return if (t.minute == 0) "$h $suffix" else "$h:${t.minute.toString().padStart(2, '0')} $suffix"
    }

    fun day(date: LocalDate, today: LocalDate): String {
        val diff = ChronoUnit.DAYS.between(today, date)
        val weekday = date.dayOfWeek.getDisplayName(TextStyle.FULL, Locale.ENGLISH)
        val month = date.month.getDisplayName(TextStyle.FULL, Locale.ENGLISH)
        return when {
            diff == 0L -> "today"
            diff == 1L -> "tomorrow"
            diff == -1L -> "yesterday"
            diff in 2..6 -> "on $weekday"
            date.year == today.year -> "on $weekday, ${date.dayOfMonth} $month"
            else -> "on ${date.dayOfMonth} $month ${date.year}"
        }
    }

    /** e.g. "tomorrow at 9 AM", "on Monday at 5:30 PM". */
    fun dateTime(dt: LocalDateTime, now: LocalDateTime): String = "${day(dt.toLocalDate(), now.toLocalDate())} at ${time(dt.toLocalTime())}"

    fun duration(d: Duration): String {
        val minutes = d.toMinutes()
        return when {
            minutes < 1 -> "${d.seconds} seconds"
            minutes == 1L -> "1 minute"
            minutes < 60 -> "$minutes minutes"
            minutes % 1440 == 0L -> (minutes / 1440).let { if (it == 1L) "1 day" else "$it days" }
            minutes % 60 == 0L -> (minutes / 60).let { if (it == 1L) "1 hour" else "$it hours" }
            minutes < 1440 -> "${minutes / 60} hour${if (minutes / 60 == 1L) "" else "s"} ${minutes % 60} minutes"
            else -> "${minutes / 1440} day${if (minutes / 1440 == 1L) "" else "s"} ${(minutes % 1440) / 60} hours"
        }
    }
}
