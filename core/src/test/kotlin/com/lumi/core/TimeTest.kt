package com.lumi.core

import com.lumi.core.time.DateTimeParser
import com.lumi.core.time.Freq
import com.lumi.core.time.Recurrence
import com.lumi.core.time.TimeDefaults
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.DayOfWeek
import java.time.Duration
import java.time.LocalDateTime

class TimeTest {
    /** Friday 13 November 2026, 10:00. */
    private val now = LocalDateTime.of(2026, 11, 13, 10, 0)

    private fun at(text: String, from: LocalDateTime = now) = DateTimeParser.parse(text, from, TimeDefaults()).dateTime

    @Test fun tomorrowUsesDefaultMorningTime() {
        val r = DateTimeParser.parse("tomorrow", now, TimeDefaults())
        assertEquals(LocalDateTime.of(2026, 11, 14, 9, 0), r.dateTime)
        assertTrue(r.hasDate)
        assertFalse(r.hasTime)
    }

    @Test fun explicitTimes() {
        assertEquals(LocalDateTime.of(2026, 11, 14, 17, 0), at("tomorrow at 5pm"))
        assertEquals(LocalDateTime.of(2026, 11, 13, 17, 45), at("5:45 pm"))
        assertEquals(LocalDateTime.of(2026, 11, 13, 12, 0), at("at noon"))
        assertEquals(LocalDateTime.of(2026, 11, 13, 18, 30), at("at half past six"))
        assertEquals(LocalDateTime.of(2026, 12, 25, 8, 0), at("on 25/12 at 8am"))
        assertEquals(LocalDateTime.of(2026, 12, 5, 12, 0), at("on december 5th at noon"))
    }

    @Test fun relativeTimes() {
        assertEquals(LocalDateTime.of(2026, 11, 13, 10, 20), at("in 20 minutes"))
        assertEquals(LocalDateTime.of(2026, 11, 13, 11, 0), at("in an hour"))
        assertEquals(LocalDateTime.of(2026, 11, 13, 10, 30), at("in half an hour"))
        assertEquals(LocalDateTime.of(2026, 11, 15, 9, 0), at("in 2 days"))
    }

    @Test fun aTimeThatAlreadyPassedTodayRollsForward() {
        // 12 AM today is in the past, so it means the coming midnight.
        assertEquals(LocalDateTime.of(2026, 11, 14, 0, 0), at("at 12 am"))
        assertEquals(LocalDateTime.of(2026, 11, 14, 8, 0), at("at 8 am"))
        // Never returns a moment in the past for a bare time.
        assertTrue(at("at 9:59 am")!!.isAfter(now))
    }

    @Test fun bareSmallHoursMeanAfternoon() {
        assertEquals(LocalDateTime.of(2026, 11, 13, 15, 0), at("at 3"))
        assertEquals(LocalDateTime.of(2026, 11, 13, 19, 0), at("at 7"))
    }

    @Test fun weekdays() {
        // "friday" said on a Friday means next week's Friday.
        assertEquals(LocalDateTime.of(2026, 11, 20, 9, 0), at("friday"))
        assertEquals(LocalDateTime.of(2026, 11, 16, 9, 0), at("next monday at 9 am"))
        assertEquals(LocalDateTime.of(2026, 11, 15, 8, 0), at("day after tomorrow morning"))
    }

    @Test fun partsOfDay() {
        assertEquals(LocalDateTime.of(2026, 11, 13, 21, 0), at("tonight"))
        assertEquals(LocalDateTime.of(2026, 11, 13, 21, 0), at("at 9 tonight"))
        assertEquals(LocalDateTime.of(2026, 11, 14, 8, 0), at("tomorrow morning"))
    }

    @Test fun monthEndIsCalendarAware() {
        val jan31 = LocalDateTime.of(2027, 1, 31, 10, 0)
        assertEquals(LocalDateTime.of(2027, 2, 28, 9, 0).toLocalDate(), at("in 1 month", jan31)!!.toLocalDate())
    }

    @Test fun recurrencePhrases() {
        val daily = DateTimeParser.parse("every day at 8 am", now, TimeDefaults())
        assertEquals(Freq.DAILY, daily.recurrence!!.freq)
        assertEquals(LocalDateTime.of(2026, 11, 14, 8, 0), daily.dateTime)

        val weekdays = DateTimeParser.parse("every weekday at 7:30", now, TimeDefaults())
        assertEquals(setOf(DayOfWeek.MONDAY, DayOfWeek.TUESDAY, DayOfWeek.WEDNESDAY, DayOfWeek.THURSDAY, DayOfWeek.FRIDAY), weekdays.recurrence!!.daysOfWeek)
        assertEquals(LocalDateTime.of(2026, 11, 16, 7, 30), weekdays.dateTime)

        val monthly = DateTimeParser.parse("every month on the 5th", now, TimeDefaults())
        assertEquals(Freq.MONTHLY, monthly.recurrence!!.freq)
        assertEquals(5, monthly.recurrence!!.dayOfMonth)
        assertEquals(LocalDateTime.of(2026, 12, 5, 9, 0), monthly.dateTime)

        val fortnight = DateTimeParser.parse("every 2 weeks", now, TimeDefaults())
        assertEquals(2, fortnight.recurrence!!.interval)
        assertEquals(2, DateTimeParser.parse("every other day", now, TimeDefaults()).recurrence!!.interval)
    }

    @Test fun remainderKeepsTheTaskText() {
        val r = DateTimeParser.parse("submit the assignment tomorrow at 5 pm", now, TimeDefaults())
        assertEquals("submit the assignment", r.remainder)
        assertEquals(LocalDateTime.of(2026, 11, 14, 17, 0), r.dateTime)
    }

    @Test fun textWithoutATimeIsNotInvented() {
        val r = DateTimeParser.parse("buy some milk", now, TimeDefaults())
        assertNull(r.dateTime)
        assertFalse(r.found)
        assertEquals("buy some milk", r.remainder)
    }

    @Test fun numbersInsideTitlesAreNotTimes() {
        val r = DateTimeParser.parse("read chapter 7 of the book", now, TimeDefaults())
        assertNull(r.dateTime)
    }

    @Test fun durations() {
        assertEquals(Duration.ofMinutes(10), DateTimeParser.parseDuration("10 minutes"))
        assertEquals(Duration.ofHours(1), DateTimeParser.parseDuration("one hour"))
        assertEquals(Duration.ofMinutes(90), DateTimeParser.parseDuration("an hour and a half"))
        assertEquals(Duration.ofMinutes(30), DateTimeParser.parseDuration("half an hour"))
        assertNull(DateTimeParser.parseDuration("a while"))
    }

    // ---- recurrence maths

    @Test fun dailyAdvancesFromAnchor() {
        val anchor = LocalDateTime.of(2026, 11, 14, 8, 0)
        val r = Recurrence(Freq.DAILY)
        assertEquals(LocalDateTime.of(2026, 11, 15, 8, 0), r.nextAfter(anchor, anchor))
        // After a long gap (phone off for days) it skips to the next future occurrence, not a backlog.
        assertEquals(LocalDateTime.of(2026, 11, 21, 8, 0), r.nextAfter(LocalDateTime.of(2026, 11, 20, 9, 0), anchor))
    }

    @Test fun weeklyWithSeveralDays() {
        val r = Recurrence(Freq.WEEKLY, daysOfWeek = setOf(DayOfWeek.MONDAY, DayOfWeek.THURSDAY))
        val anchor = r.firstOnOrAfter(LocalDateTime.of(2026, 11, 13, 9, 0))
        assertEquals(LocalDateTime.of(2026, 11, 16, 9, 0), anchor)
        assertEquals(LocalDateTime.of(2026, 11, 19, 9, 0), r.nextAfter(anchor, anchor))
        assertEquals(LocalDateTime.of(2026, 11, 23, 9, 0), r.nextAfter(LocalDateTime.of(2026, 11, 19, 9, 0), anchor))
    }

    @Test fun everyOtherWeekKeepsItsPhase() {
        val r = Recurrence(Freq.WEEKLY, interval = 2)
        val anchor = LocalDateTime.of(2026, 11, 13, 9, 0)
        assertEquals(LocalDateTime.of(2026, 11, 27, 9, 0), r.nextAfter(anchor, anchor))
        assertEquals(LocalDateTime.of(2026, 12, 11, 9, 0), r.nextAfter(LocalDateTime.of(2026, 11, 30, 0, 0), anchor))
    }

    @Test fun monthlyOnThe31stClampsToShortMonths() {
        val r = Recurrence(Freq.MONTHLY, dayOfMonth = 31)
        val anchor = LocalDateTime.of(2027, 1, 31, 9, 0)
        assertEquals(LocalDateTime.of(2027, 2, 28, 9, 0), r.nextAfter(anchor, anchor))
        assertEquals(LocalDateTime.of(2027, 3, 31, 9, 0), r.nextAfter(LocalDateTime.of(2027, 2, 28, 9, 0), anchor))
    }

    @Test fun encodeDecodeRoundTrip() {
        val all = listOf(
            Recurrence(Freq.DAILY),
            Recurrence(Freq.WEEKLY, 2, setOf(DayOfWeek.MONDAY, DayOfWeek.FRIDAY)),
            Recurrence(Freq.MONTHLY, 1, dayOfMonth = 15),
            Recurrence(Freq.HOURLY, 3),
            Recurrence(Freq.YEARLY),
        )
        for (r in all) assertEquals(r, Recurrence.decode(r.encode()))
        assertNull(Recurrence.decode("nonsense"))
        assertNull(Recurrence.decode(null))
        assertNotNull(Recurrence(Freq.DAILY).describe())
    }
}
