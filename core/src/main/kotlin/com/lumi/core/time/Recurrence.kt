package com.lumi.core.time

import java.time.DayOfWeek
import java.time.Duration
import java.time.LocalDateTime
import java.time.YearMonth
import java.time.temporal.ChronoUnit
import java.time.temporal.TemporalAdjusters

enum class Freq { MINUTELY, HOURLY, DAILY, WEEKLY, MONTHLY, YEARLY }

/**
 * A repeating schedule. Occurrences keep the local time-of-day of the anchor, so a
 * "every day at 8" reminder stays at 8 across daylight-saving changes.
 */
data class Recurrence(
    val freq: Freq,
    val interval: Int = 1,
    val daysOfWeek: Set<DayOfWeek> = emptySet(),
    val dayOfMonth: Int? = null,
) {
    init {
        require(interval >= 1) { "interval must be >= 1" }
        require(dayOfMonth == null || dayOfMonth in 1..31) { "dayOfMonth out of range" }
    }

    /** First occurrence that is not before [anchor] and satisfies the rule. */
    fun firstOnOrAfter(anchor: LocalDateTime): LocalDateTime = when (freq) {
        Freq.WEEKLY -> if (daysOfWeek.isEmpty() || anchor.dayOfWeek in daysOfWeek) anchor else nextWeekly(anchor, anchor)
        Freq.MONTHLY -> {
            val dom = dayOfMonth
            if (dom == null || anchor.dayOfMonth == minOf(dom, YearMonth.from(anchor).lengthOfMonth())) anchor
            else nextMonthly(anchor.minusSeconds(1), anchor, allowAnchorMonthLater = true)
        }
        else -> anchor
    }

    /** The next occurrence strictly after [after], for a series that started at [anchor]. */
    fun nextAfter(after: LocalDateTime, anchor: LocalDateTime): LocalDateTime {
        val start = firstOnOrAfter(anchor)
        if (after.isBefore(start)) return start
        return when (freq) {
            Freq.MINUTELY -> stepFixed(after, start, Duration.ofMinutes(interval.toLong()))
            Freq.HOURLY -> stepFixed(after, start, Duration.ofHours(interval.toLong()))
            Freq.DAILY -> {
                val days = ChronoUnit.DAYS.between(start.toLocalDate(), after.toLocalDate())
                var k = (days / interval) * interval
                var candidate = start.plusDays(k)
                while (!candidate.isAfter(after)) {
                    k += interval
                    candidate = start.plusDays(k)
                }
                candidate
            }
            Freq.WEEKLY -> nextWeekly(after, start)
            Freq.MONTHLY -> nextMonthly(after, start, allowAnchorMonthLater = false)
            Freq.YEARLY -> {
                var k = 0L
                var candidate = start
                while (!candidate.isAfter(after)) {
                    k += interval
                    candidate = start.plusYears(k)
                }
                candidate
            }
        }
    }

    private fun stepFixed(after: LocalDateTime, start: LocalDateTime, step: Duration): LocalDateTime {
        val elapsed = Duration.between(start, after)
        val n = elapsed.toMillis() / step.toMillis() + 1
        return start.plus(step.multipliedBy(n))
    }

    private fun nextWeekly(after: LocalDateTime, start: LocalDateTime): LocalDateTime {
        val days = if (daysOfWeek.isEmpty()) setOf(start.dayOfWeek) else daysOfWeek
        val startWeek = start.toLocalDate().with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY))
        var date = if (after.toLocalDate().isBefore(start.toLocalDate())) start.toLocalDate() else after.toLocalDate()
        // Bounded scan: at most `interval` weeks plus one to find the next matching day.
        repeat(7 * (interval + 1) + 1) {
            val candidate = date.atTime(start.toLocalTime())
            val weekStart = date.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY))
            val weeks = ChronoUnit.WEEKS.between(startWeek, weekStart)
            if (date.dayOfWeek in days && weeks % interval == 0L && candidate.isAfter(after) && !candidate.isBefore(start)) {
                return candidate
            }
            date = date.plusDays(1)
        }
        return start.plusWeeks(interval.toLong()) // unreachable for valid input
    }

    private fun nextMonthly(after: LocalDateTime, start: LocalDateTime, allowAnchorMonthLater: Boolean): LocalDateTime {
        val dom = dayOfMonth ?: start.dayOfMonth
        val startMonth = YearMonth.from(start)
        var k = 0L
        if (!allowAnchorMonthLater) {
            val months = ChronoUnit.MONTHS.between(startMonth, YearMonth.from(after))
            k = maxOf(0L, (months / interval) * interval)
        }
        while (true) {
            val ym = startMonth.plusMonths(k)
            val candidate = ym.atDay(minOf(dom, ym.lengthOfMonth())).atTime(start.toLocalTime())
            if (candidate.isAfter(after) && (allowAnchorMonthLater || !candidate.isBefore(start))) return candidate
            k += interval
        }
    }

    /** Compact, stable text form used for persistence, e.g. `FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TH`. */
    fun encode(): String = buildString {
        append("FREQ=").append(freq.name)
        append(";INTERVAL=").append(interval)
        if (daysOfWeek.isNotEmpty()) {
            append(";BYDAY=").append(daysOfWeek.sorted().joinToString(",") { it.name.take(2) })
        }
        if (dayOfMonth != null) append(";BYMONTHDAY=").append(dayOfMonth)
    }

    /** Human readable form, e.g. "every Monday and Thursday". */
    fun describe(): String {
        fun unit(singular: String) = if (interval == 1) "every $singular" else if (interval == 2) "every other $singular" else "every $interval ${singular}s"
        return when (freq) {
            Freq.MINUTELY -> if (interval == 1) "every minute" else "every $interval minutes"
            Freq.HOURLY -> if (interval == 1) "every hour" else "every $interval hours"
            Freq.DAILY -> unit("day")
            Freq.WEEKLY -> when {
                daysOfWeek.isEmpty() -> unit("week")
                daysOfWeek == WEEKDAYS -> if (interval == 1) "every weekday" else "weekdays every $interval weeks"
                daysOfWeek == WEEKEND -> if (interval == 1) "every weekend" else "weekends every $interval weeks"
                else -> {
                    val names = daysOfWeek.sorted().map { it.name.lowercase().replaceFirstChar { c -> c.uppercase() } }
                    val list = if (names.size == 1) names[0] else names.dropLast(1).joinToString(", ") + " and " + names.last()
                    if (interval == 1) "every $list" else if (interval == 2) "every other $list" else "every $interval weeks on $list"
                }
            }
            Freq.MONTHLY -> unit("month") + (dayOfMonth?.let { " on day $it" } ?: "")
            Freq.YEARLY -> unit("year")
        }
    }

    companion object {
        val WEEKDAYS: Set<DayOfWeek> = setOf(DayOfWeek.MONDAY, DayOfWeek.TUESDAY, DayOfWeek.WEDNESDAY, DayOfWeek.THURSDAY, DayOfWeek.FRIDAY)
        val WEEKEND: Set<DayOfWeek> = setOf(DayOfWeek.SATURDAY, DayOfWeek.SUNDAY)

        /** Parses the output of [encode]. Returns null for blank or malformed input. */
        fun decode(text: String?): Recurrence? {
            if (text.isNullOrBlank()) return null
            return try {
                val parts = text.split(';').mapNotNull {
                    val i = it.indexOf('=')
                    if (i <= 0) null else it.substring(0, i).trim().uppercase() to it.substring(i + 1).trim()
                }.toMap()
                val freq = Freq.valueOf(parts["FREQ"]?.uppercase() ?: return null)
                val interval = parts["INTERVAL"]?.toInt() ?: 1
                val days = parts["BYDAY"]?.split(',')?.filter { it.isNotBlank() }?.map { code ->
                    DayOfWeek.values().first { it.name.startsWith(code.trim().uppercase()) }
                }?.toSet() ?: emptySet()
                Recurrence(freq, interval, days, parts["BYMONTHDAY"]?.toInt())
            } catch (e: Exception) {
                null
            }
        }
    }
}
