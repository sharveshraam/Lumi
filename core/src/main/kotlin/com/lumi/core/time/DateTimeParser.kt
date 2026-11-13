package com.lumi.core.time

import java.time.DayOfWeek
import java.time.Duration
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.YearMonth
import java.time.temporal.TemporalAdjusters

/** Default clock times used when the user names only a date or a part of the day. */
data class TimeDefaults(
    val defaultTime: LocalTime = LocalTime.of(9, 0),
    val morning: LocalTime = LocalTime.of(8, 0),
    val afternoon: LocalTime = LocalTime.of(15, 0),
    val evening: LocalTime = LocalTime.of(18, 0),
    val night: LocalTime = LocalTime.of(21, 0),
)

/** Result of extracting temporal information from free text. */
data class TimeParse(
    /** Resolved moment; for recurring schedules this is the first occurrence. Null when nothing temporal was found. */
    val dateTime: LocalDateTime?,
    val recurrence: Recurrence?,
    /** The input with temporal phrases removed, suitable as a reminder title. */
    val remainder: String,
    val hasDate: Boolean,
    val hasTime: Boolean,
) {
    val found: Boolean get() = dateTime != null || recurrence != null
}

/**
 * Deterministic natural-language date/time extraction. It is intentionally conservative:
 * a bare number is never treated as a time unless it has "at"/"am"/"pm"/":" context.
 */
object DateTimeParser {
    private const val MARK = '\u0000'
    private val I = setOf(RegexOption.IGNORE_CASE)

    private const val NUM =
        "(\\d+(?:\\.\\d+)?|half\\s+an?|a\\s+couple\\s+of|couple\\s+of|a\\s+few|forty[- ]five|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety|an?)"
    private const val UNIT = "(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)"
    private const val PART = "(morning|afternoon|evening|night)"
    private const val DAY = "(monday|tuesday|tues|wednesday|thursday|thurs|friday|saturday|sunday)"
    private const val DAYS_PLURAL = "(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?"
    private const val MONTH =
        "(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    private const val HOUR_WORD = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    private const val MERIDIEM = "(a\\.?m\\.?|p\\.?m\\.?)(?![a-z])"

    private fun rx(p: String) = Regex(p, I)

    private val rEveryWeekdayKind = rx("\\b(?:every|each|on)\\s+(week\\s*day|weekend)s?\\b")
    private val rEveryPart = rx("\\b(?:every|each)\\s+$PART\\b")
    private val rEveryDays = rx("\\b(?:every|each)\\s+(other\\s+|second\\s+)?($DAYS_PLURAL(?:\\s*(?:,|and|&|,\\s*and)\\s*$DAYS_PLURAL)*)\\b(?:\\s+$PART\\b)?")
    private val rOnDaysPlural = rx("\\bon\\s+((?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s(?:\\s*(?:,|and|&|,\\s*and)\\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s)*)\\b")
    private val rEveryUnit = rx("\\b(?:every|each)\\s+(?:(other|second|\\d+|two|three|four|five|six|ten|fifteen|twenty|thirty)\\s+)?(minute|hour|day|week|month|year)s?\\b")
    private val rSingleWord = rx("\\b(everyday|daily|weekly|monthly|yearly|annually|hourly|fortnightly)\\b")
    private val rMonthDay = rx("\\bon\\s+(?:the\\s+)?(\\d{1,2})(?:st|nd|rd|th)?\\b(?!\\s*[:/])")

    private val rHourAndHalf = rx("\\b(?:in|after)\\s+(?:an?|one)\\s+hour\\s+and\\s+a\\s+half\\b")
    private val rNumAndHalf = rx("\\b(?:in|after)\\s+$NUM\\s+and\\s+a\\s+half\\s+(hours?|hrs?|minutes?|mins?|days?)\\b")
    private val rIn = rx("\\b(?:in|after)\\s+$NUM\\s*$UNIT\\b(?:\\s+(?:and\\s+)?$NUM\\s*$UNIT\\b)?(?:\\s+(?:from\\s+now|time))?")
    private val rFromNow = rx("\\b$NUM\\s*$UNIT\\s+(?:from\\s+now|later)\\b")

    private val rDateDayMonth = rx("\\b(?:on\\s+|by\\s+)?(?:the\\s+)?(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?$MONTH\\b(?:,?\\s+(\\d{4})\\b)?")
    private val rDateMonthDay = rx("\\b(?:on\\s+|by\\s+)?$MONTH\\s+(?:the\\s+)?(\\d{1,2})(?:st|nd|rd|th)?\\b(?!\\s*[:.]\\d)(?:,?\\s+(\\d{4})\\b)?")
    private val rDateNumericYear = rx("\\b(?:on\\s+|by\\s+)?(\\d{1,2})[/-](\\d{1,2})[/-](\\d{4}|\\d{2})\\b")
    private val rDateNumeric = rx("\\b(?:on|by)\\s+(\\d{1,2})/(\\d{1,2})\\b")
    private val rDateOrdinal = rx("\\b(?:on|by)\\s+(?:the\\s+)?(\\d{1,2})(?:st|nd|rd|th)\\b")

    private val rDayAfterTomorrow = rx("\\b(?:the\\s+)?day\\s+after\\s+tomorrow\\b(?:\\s+$PART\\b)?")
    private val rTomorrow = rx("\\b(?:tomorrow|tmrw)\\b(?:\\s+$PART\\b)?")
    private val rTonight = rx("\\btonight\\b")
    private val rToday = rx("\\b(?:later\\s+)?today\\b(?:\\s+$PART\\b)?")
    private val rWeekday = rx("\\b(?:on\\s+|this\\s+|coming\\s+|this\\s+coming\\s+)?(next\\s+)?$DAY\\b(?:\\s+$PART\\b)?")
    private val rNextUnit = rx("\\bnext\\s+(week|month|year)\\b")
    private val rWeekend = rx("\\b(?:this|on\\s+the|at\\s+the|over\\s+the)\\s+weekend\\b")

    private val rTimeColon = rx("(?:\\b(?:at|by|around)\\s+)?\\b(\\d{1,2}):(\\d{2})(?:\\s*$MERIDIEM)?")
    private val rTimeDotMeridiem = rx("(?:\\b(?:at|by|around)\\s+)?\\b(\\d{1,2})\\.(\\d{2})\\s*$MERIDIEM")
    private val rTimeAtDot = rx("\\b(?:at|by|around)\\s+(\\d{1,2})\\.(\\d{2})\\b")
    private val rTimeMeridiem = rx("(?:\\b(?:at|by|around)\\s+)?\\b(\\d{1,2}|$HOUR_WORD)\\s*$MERIDIEM")
    private val rHalfPast = rx("(?:\\b(?:at|by|around)\\s+)?\\b(half|quarter)\\s+(past|to)\\s+(\\d{1,2}|$HOUR_WORD)\\b(?:\\s*$MERIDIEM)?")
    private val rOClock = rx("(?:\\b(?:at|by|around)\\s+)?\\b(\\d{1,2}|$HOUR_WORD)\\s*o'?\\s?clock\\b")
    private val rAtHour = rx("\\b(?:at|by|around)\\s+(\\d{1,2}|$HOUR_WORD)\\b(?!\\s*(?:%|:|/|\\.\\d|st\\b|nd\\b|rd\\b|th\\b|percent|minutes?|mins?|hours?|days?|weeks?|months?|times?|people|persons?))")
    private val rNoon = rx("(?:\\b(?:at|by|around)\\s+)?\\b(noon|midday|midnight)\\b")
    private val rEndOfDay = rx("\\b(?:by\\s+)?(?:the\\s+)?end\\s+of\\s+(?:the\\s+)?day\\b")
    private val rPart = rx("\\b(?:in\\s+the|this|at|during\\s+the|by|for\\s+the)\\s+$PART\\b")

    private val rDurationPiece = rx("\\b$NUM\\s*$UNIT\\b")
    private val rDurHourHalf = rx("\\b(?:an?|one)\\s+hour\\s+and\\s+a\\s+half\\b")

    private class Clock(val hour: Int, val minute: Int, val meridiem: Char?)
    private class Scan(var s: String) {
        fun take(r: Regex): MatchResult? {
            val m = r.find(s) ?: return null
            s = s.substring(0, m.range.first) + MARK + s.substring(m.range.last + 1)
            return m
        }
    }

    fun parse(text: String, now: LocalDateTime, defaults: TimeDefaults = TimeDefaults()): TimeParse {
        val scan = Scan(text)
        val today = now.toLocalDate()

        var recurrence: Recurrence? = null
        var part: String? = null
        var date: LocalDate? = null
        var weekdayTarget: DayOfWeek? = null
        var relative: Duration? = null
        var clock: Clock? = null
        var fixedTime: LocalTime? = null
        var midnight = false

        // 1. Recurrence phrases.
        scan.take(rEveryWeekdayKind)?.let { m ->
            recurrence = Recurrence(Freq.WEEKLY, 1, if (m.groupValues[1].lowercase().startsWith("weekend")) Recurrence.WEEKEND else Recurrence.WEEKDAYS)
        }
        if (recurrence == null) scan.take(rEveryPart)?.let { m ->
            recurrence = Recurrence(Freq.DAILY)
            part = m.groupValues[1].lowercase()
        }
        if (recurrence == null) scan.take(rEveryDays)?.let { m ->
            recurrence = Recurrence(Freq.WEEKLY, if (m.groupValues[1].isNotBlank()) 2 else 1, daysIn(m.groupValues[2]))
            if (m.groupValues[3].isNotBlank()) part = m.groupValues[3].lowercase()
        }
        if (recurrence == null) scan.take(rOnDaysPlural)?.let { m ->
            recurrence = Recurrence(Freq.WEEKLY, 1, daysIn(m.groupValues[1]))
        }
        if (recurrence == null) scan.take(rEveryUnit)?.let { m ->
            val n = when (val q = m.groupValues[1].lowercase()) {
                "" -> 1
                "other", "second" -> 2
                else -> numberValue(q)?.toInt() ?: 1
            }.coerceAtLeast(1)
            val freq = when (m.groupValues[2].lowercase()) {
                "minute" -> Freq.MINUTELY
                "hour" -> Freq.HOURLY
                "day" -> Freq.DAILY
                "week" -> Freq.WEEKLY
                "month" -> Freq.MONTHLY
                else -> Freq.YEARLY
            }
            recurrence = Recurrence(freq, n)
        }
        if (recurrence == null) scan.take(rSingleWord)?.let { m ->
            recurrence = when (m.groupValues[1].lowercase()) {
                "everyday", "daily" -> Recurrence(Freq.DAILY)
                "weekly" -> Recurrence(Freq.WEEKLY)
                "fortnightly" -> Recurrence(Freq.WEEKLY, 2)
                "monthly" -> Recurrence(Freq.MONTHLY)
                "hourly" -> Recurrence(Freq.HOURLY)
                else -> Recurrence(Freq.YEARLY)
            }
        }
        recurrence?.let { rec ->
            if (rec.freq == Freq.MONTHLY) {
                val m = rMonthDay.find(scan.s)
                val dom = m?.groupValues?.get(1)?.toIntOrNull()
                if (m != null && dom != null && dom in 1..31) {
                    scan.take(rMonthDay)
                    recurrence = rec.copy(dayOfMonth = dom)
                }
            }
        }

        // 2. Relative offsets ("in 30 minutes").
        if (scan.take(rHourAndHalf) != null) {
            relative = Duration.ofMinutes(90)
        } else {
            val half = scan.take(rNumAndHalf)
            if (half != null) {
                relative = durationOf((numberValue(half.groupValues[1]) ?: 1.0) + 0.5, half.groupValues[2])
            } else {
                val m = scan.take(rIn)
                if (m != null) {
                    val n = numberValue(m.groupValues[1]) ?: 1.0
                    val u = m.groupValues[2].lowercase()
                    val calendarUnit = (u.startsWith("mo") || u.startsWith("y")) && m.groupValues[3].isBlank()
                    if (calendarUnit) {
                        // Calendar-aware: "in 2 months" lands on the same day of the month.
                        date = if (u.startsWith("mo")) today.plusMonths(n.toLong()) else today.plusYears(n.toLong())
                    } else {
                        var d = durationOf(n, u)
                        if (m.groupValues[3].isNotBlank()) d = d.plus(durationOf(numberValue(m.groupValues[3]) ?: 1.0, m.groupValues[4]))
                        relative = d
                    }
                } else {
                    scan.take(rFromNow)?.let { relative = durationOf(numberValue(it.groupValues[1]) ?: 1.0, it.groupValues[2]) }
                }
            }
        }
        relative?.let { rel ->
            if (rel.seconds % 86_400 == 0L && rel.seconds > 0) {
                date = today.plusDays(rel.seconds / 86_400)
                relative = null
            }
        }

        // 3. Explicit calendar dates.
        if (date == null) {
            scan.take(rDateNumericYear)?.let { m -> date = buildDate(m.groupValues[1], monthNumber = m.groupValues[2].toIntOrNull(), year = m.groupValues[3], today = today) }
        }
        if (date == null) {
            val m = rDateDayMonth.find(scan.s)
            if (m != null) {
                val d = buildDate(m.groupValues[1], monthIndex(m.groupValues[2]), m.groupValues[3], today)
                if (d != null) { scan.take(rDateDayMonth); date = d }
            }
        }
        if (date == null) {
            val m = rDateMonthDay.find(scan.s)
            if (m != null) {
                val d = buildDate(m.groupValues[2], monthIndex(m.groupValues[1]), m.groupValues[3], today)
                if (d != null) { scan.take(rDateMonthDay); date = d }
            }
        }
        if (date == null) {
            val m = rDateNumeric.find(scan.s)
            if (m != null) {
                val d = buildDate(m.groupValues[1], m.groupValues[2].toIntOrNull(), "", today)
                if (d != null) { scan.take(rDateNumeric); date = d }
            }
        }
        if (date == null && recurrence == null) {
            val m = rDateOrdinal.find(scan.s)
            val dom = m?.groupValues?.get(1)?.toIntOrNull()
            if (m != null && dom != null && dom in 1..31) {
                scan.take(rDateOrdinal)
                var ym = YearMonth.from(today)
                var guard = 0
                while ((dom > ym.lengthOfMonth() || !ym.atDay(dom).isAfter(today)) && guard++ < 14) ym = ym.plusMonths(1)
                date = ym.atDay(minOf(dom, ym.lengthOfMonth()))
            }
        }

        // 4. Day keywords.
        scan.take(rDayAfterTomorrow)?.let { m ->
            if (date == null) date = today.plusDays(2)
            if (m.groupValues[1].isNotBlank()) part = m.groupValues[1].lowercase()
        }
        scan.take(rTomorrow)?.let { m ->
            if (date == null) date = today.plusDays(1)
            if (m.groupValues[1].isNotBlank()) part = m.groupValues[1].lowercase()
        }
        scan.take(rTonight)?.let {
            if (date == null) date = today
            part = "night"
        }
        scan.take(rToday)?.let { m ->
            if (date == null) date = today
            if (m.groupValues[1].isNotBlank()) part = m.groupValues[1].lowercase()
        }
        if (date == null && recurrence == null) {
            scan.take(rWeekday)?.let { m ->
                val target = dayOf(m.groupValues[2])
                if (m.groupValues[1].isNotBlank()) {
                    val nextMonday = today.with(TemporalAdjusters.next(DayOfWeek.MONDAY))
                    date = nextMonday.with(TemporalAdjusters.nextOrSame(target))
                } else {
                    weekdayTarget = target
                    date = today.with(TemporalAdjusters.nextOrSame(target))
                }
                if (m.groupValues[3].isNotBlank()) part = m.groupValues[3].lowercase()
            }
        }
        if (date == null) {
            scan.take(rNextUnit)?.let { m ->
                date = when (m.groupValues[1].lowercase()) {
                    "week" -> today.plusWeeks(1)
                    "month" -> today.plusMonths(1)
                    else -> today.plusYears(1)
                }
            }
        }
        if (date == null) {
            scan.take(rWeekend)?.let {
                date = if (today.dayOfWeek == DayOfWeek.SATURDAY || today.dayOfWeek == DayOfWeek.SUNDAY) today
                else today.with(TemporalAdjusters.next(DayOfWeek.SATURDAY))
            }
        }

        // 5. Clock times.
        val subDayRelative = relative != null
        if (!subDayRelative) {
            fun meridiemOf(s: String): Char? = s.firstOrNull()?.lowercaseChar()?.takeIf { it == 'a' || it == 'p' }
            fun validClock(h: Int?, min: Int?, mer: Char?): Clock? {
                if (h == null || min == null || min !in 0..59) return null
                if (mer != null && h !in 1..12) return null
                if (h !in 0..23) return null
                return Clock(h, min, mer)
            }
            fun tryTake(r: Regex, build: (MatchResult) -> Clock?) {
                if (clock != null) return
                val m = r.find(scan.s) ?: return
                val c = build(m) ?: return
                scan.take(r)
                clock = c
            }
            tryTake(rTimeColon) { validClock(it.groupValues[1].toIntOrNull(), it.groupValues[2].toIntOrNull(), meridiemOf(it.groupValues[3])) }
            tryTake(rTimeDotMeridiem) { validClock(it.groupValues[1].toIntOrNull(), it.groupValues[2].toIntOrNull(), meridiemOf(it.groupValues[3])) }
            tryTake(rTimeAtDot) { validClock(it.groupValues[1].toIntOrNull(), it.groupValues[2].toIntOrNull(), null) }
            tryTake(rTimeMeridiem) { validClock(hourValue(it.groupValues[1]), 0, meridiemOf(it.groupValues[2])) }
            tryTake(rHalfPast) { m ->
                val base = hourValue(m.groupValues[3]) ?: return@tryTake null
                val mer = meridiemOf(m.groupValues[4])
                val isTo = m.groupValues[2].equals("to", true)
                val minute = if (m.groupValues[1].equals("half", true)) 30 else if (isTo) 45 else 15
                val hour = if (isTo) (if (base == 1) 12 else base - 1) else base
                validClock(hour, minute, mer)
            }
            tryTake(rOClock) { validClock(hourValue(it.groupValues[1]), 0, null) }
            tryTake(rAtHour) { validClock(hourValue(it.groupValues[1]), 0, null) }
            if (clock == null) {
                scan.take(rNoon)?.let { m ->
                    if (m.groupValues[1].equals("midnight", true)) { fixedTime = LocalTime.MIDNIGHT; midnight = true } else fixedTime = LocalTime.NOON
                }
            }
            if (clock == null && fixedTime == null) scan.take(rEndOfDay)?.let {
                if (date == null) date = today
                fixedTime = defaults.evening
            }
            scan.take(rPart)?.let { part = it.groupValues[1].lowercase() }
        }

        // 6. Resolve everything into a single moment.
        val isToday = date == null || date == today
        var time: LocalTime? = fixedTime
        clock?.let { c ->
            time = when {
                c.meridiem == 'a' -> LocalTime.of(c.hour % 12, c.minute)
                c.meridiem == 'p' -> LocalTime.of(c.hour % 12 + 12, c.minute)
                c.hour == 0 || c.hour > 12 -> LocalTime.of(c.hour, c.minute)
                part == "morning" -> LocalTime.of(c.hour % 12, c.minute)
                part == "afternoon" || part == "evening" -> LocalTime.of(c.hour % 12 + 12, c.minute)
                part == "night" -> if (c.hour in 6..11) LocalTime.of(c.hour + 12, c.minute) else LocalTime.of(c.hour % 12, c.minute)
                isToday && weekdayTarget == null -> {
                    val am = LocalTime.of(c.hour % 12, c.minute)
                    val pm = LocalTime.of(c.hour % 12 + 12, c.minute)
                    val current = now.toLocalTime()
                    if (date == null && recurrence != null) {
                        // "every day at 8": prefer a sensible waking hour over the nearest clock match.
                        if (c.hour in 1..5) pm else am
                    } else if (am.isAfter(current)) am else if (pm.isAfter(current)) pm else am
                }
                c.hour in 1..6 || c.hour == 12 -> LocalTime.of(c.hour % 12 + 12, c.minute)
                else -> LocalTime.of(c.hour, c.minute)
            }
        }
        val hasExplicitTime = time != null
        if (time == null && part != null) {
            time = when (part) {
                "morning" -> defaults.morning
                "afternoon" -> defaults.afternoon
                "evening" -> defaults.evening
                else -> defaults.night
            }
        }

        var result: LocalDateTime? = null
        var hasDate = date != null
        var hasTime = time != null
        val rel = relative
        if (rel != null) {
            result = now.withNano(0).plus(rel)
            hasDate = true
            hasTime = true
        } else {
            val target = weekdayTarget
            var d = date
            val t = time
            if (d != null && target != null && d == today) {
                // "on Monday" said on a Monday: today only if a later time today was given.
                if (t == null || !today.atTime(t).isAfter(now)) d = today.plusWeeks(1)
            }
            if (d != null && t != null) {
                result = d.atTime(t)
                if (midnight && date == today) result = today.plusDays(1).atStartOfDay()
                if (d == today && !result.isAfter(now) && !hasExplicitTime && recurrence == null) {
                    // "this morning" said in the afternoon: fall forward rather than schedule in the past.
                    result = if (part == "night") now.withNano(0).plusHours(1) else result.plusDays(1)
                }
            } else if (d != null) {
                result = d.atTime(defaults.defaultTime)
                if (d == today && !result.isAfter(now)) {
                    val eveningToday = today.atTime(defaults.evening)
                    result = if (eveningToday.isAfter(now.plusMinutes(30))) eveningToday else now.withNano(0).withSecond(0).plusHours(1)
                }
            } else if (t != null) {
                result = today.atTime(t)
                if (!result.isAfter(now)) result = result.plusDays(1)
            }
        }

        val rec = recurrence
        if (rec != null) {
            val anchor = result ?: when (rec.freq) {
                Freq.MINUTELY -> now.withNano(0).plusMinutes(rec.interval.toLong())
                Freq.HOURLY -> now.withNano(0).plusHours(rec.interval.toLong())
                else -> today.atTime(defaults.defaultTime)
            }
            var first = rec.firstOnOrAfter(anchor)
            if (!first.isAfter(now)) first = rec.nextAfter(now, anchor)
            result = first
        }

        return TimeParse(result, rec, cleanRemainder(scan.s), hasDate || rec != null, hasTime)
    }

    /** Parses a plain duration such as "one hour", "45 minutes" or "an hour and a half". */
    fun parseDuration(text: String): Duration? {
        var s = text
        var total = Duration.ZERO
        var any = false
        rDurHourHalf.find(s)?.let {
            total = total.plusMinutes(90)
            any = true
            s = s.removeRange(it.range)
        }
        for (m in rDurationPiece.findAll(s)) {
            val n = numberValue(m.groupValues[1]) ?: continue
            total = total.plus(durationOf(n, m.groupValues[2]))
            any = true
        }
        return if (any && !total.isZero) total else null
    }

    private fun cleanRemainder(marked: String): String {
        var s = marked
        val dangling = Regex("(?:\\b(?:at|on|by|around|until|till|starting|from|this|coming|and)\\b|,)\\s*\\x00", I)
        while (true) {
            val next = dangling.replace(s, MARK.toString())
            if (next == s) break
            s = next
        }
        s = s.replace(MARK, ' ').replace(Regex("\\s+"), " ").trim()
        s = s.replace(Regex("\\s+([,.!?])"), "$1")
        return s.trim(' ', ',', '.', '-', ';', ':', '!', '?')
    }

    private fun daysIn(text: String): Set<DayOfWeek> =
        Regex("(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", I).findAll(text).map { dayOf(it.value) }.toSet()

    private fun dayOf(name: String): DayOfWeek = when (name.lowercase().take(3)) {
        "mon" -> DayOfWeek.MONDAY
        "tue" -> DayOfWeek.TUESDAY
        "wed" -> DayOfWeek.WEDNESDAY
        "thu" -> DayOfWeek.THURSDAY
        "fri" -> DayOfWeek.FRIDAY
        "sat" -> DayOfWeek.SATURDAY
        else -> DayOfWeek.SUNDAY
    }

    private val monthPrefixes = listOf("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    private fun monthIndex(name: String): Int? = monthPrefixes.indexOf(name.lowercase().take(3)).takeIf { it >= 0 }?.plus(1)

    private fun buildDate(day: String, monthNumber: Int?, year: String, today: LocalDate): LocalDate? {
        val d = day.toIntOrNull() ?: return null
        val m = monthNumber ?: return null
        if (m !in 1..12 || d !in 1..31) return null
        return try {
            if (year.isNotBlank()) {
                val y = year.toInt().let { if (it < 100) 2000 + it else it }
                LocalDate.of(y, m, d)
            } else {
                val thisYear = LocalDate.of(today.year, m, d)
                if (thisYear.isBefore(today)) LocalDate.of(today.year + 1, m, d) else thisYear
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun hourValue(text: String): Int? {
        text.toIntOrNull()?.let { return it }
        val i = HOUR_WORD.split('|').indexOf(text.lowercase())
        return if (i >= 0) i + 1 else null
    }

    internal fun numberValue(text: String): Double? {
        val t = text.trim().lowercase().replace(Regex("\\s+"), " ")
        t.toDoubleOrNull()?.let { return it }
        return when (t) {
            "a", "an", "one" -> 1.0
            "half a", "half an" -> 0.5
            "a couple of", "couple of", "two" -> 2.0
            "a few", "three" -> 3.0
            "four" -> 4.0
            "five" -> 5.0
            "six" -> 6.0
            "seven" -> 7.0
            "eight" -> 8.0
            "nine" -> 9.0
            "ten" -> 10.0
            "eleven" -> 11.0
            "twelve" -> 12.0
            "fifteen" -> 15.0
            "twenty" -> 20.0
            "thirty" -> 30.0
            "forty" -> 40.0
            "forty five", "forty-five" -> 45.0
            "fifty" -> 50.0
            "sixty" -> 60.0
            "ninety" -> 90.0
            else -> null
        }
    }

    private fun durationOf(n: Double, unit: String): Duration {
        val u = unit.lowercase()
        val seconds = when {
            u.startsWith("sec") -> 1.0
            u.startsWith("min") -> 60.0
            u.startsWith("h") -> 3600.0
            u.startsWith("d") -> 86_400.0
            u.startsWith("w") -> 604_800.0
            u.startsWith("mo") -> 2_592_000.0
            else -> 31_536_000.0
        }
        return Duration.ofSeconds(Math.round(n * seconds))
    }
}
