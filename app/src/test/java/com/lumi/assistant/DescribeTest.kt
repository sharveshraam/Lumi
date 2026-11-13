package com.lumi.assistant

import com.lumi.assistant.ui.Describe
import com.lumi.assistant.ui.Screens
import org.junit.Assert.assertEquals
import org.junit.Test

class DescribeTest {
    @Test
    fun clockFormatsMinutesOfDay() {
        assertEquals(Describe.clock(0), Describe.clock(1440 % 1440))
        assertEquals(com.lumi.core.time.TimeFormat.time(java.time.LocalTime.of(7, 30)), Describe.clock(7 * 60 + 30))
    }

    @Test
    fun unknownScreensFallBackToHome() {
        assertEquals("home", Screens.valid(null))
        assertEquals("home", Screens.valid("nope"))
        assertEquals("tasks", Screens.valid("tasks"))
        assertEquals("permissions", Screens.valid("permissions"))
    }
}
