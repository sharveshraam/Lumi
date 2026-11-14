package com.lumi.core

import com.lumi.core.briefing.BriefingComposer
import com.lumi.core.briefing.BriefingData
import com.lumi.core.briefing.BriefingOptions
import com.lumi.core.model.Task
import com.lumi.core.model.now
import com.lumi.core.model.toMillis
import com.lumi.core.notify.NotificationIntel
import com.lumi.core.notify.NotificationItem
import com.lumi.core.notify.NotificationPrefs
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class NotificationAndBriefingTest {
    private val on = NotificationPrefs(enabled = true)

    private fun item(
        key: String = "k",
        pkg: String = "com.whatsapp",
        app: String = "WhatsApp",
        title: String = "Priya",
        text: String = "Are we still on for lunch?",
        at: Long = 1000,
        message: Boolean = true,
        ongoing: Boolean = false,
        high: Boolean = false,
        summary: Boolean = false,
    ) = NotificationItem(key, pkg, app, title, text, at, isMessage = message, isOngoing = ongoing, canReply = message, highPriority = high, isGroupSummary = summary)

    @Test
    fun nothingIsUsableUntilTheUserOptsIn() {
        assertFalse(NotificationIntel.allowed(item(), NotificationPrefs()))
        assertTrue(NotificationIntel.usable(listOf(item()), NotificationPrefs()).isEmpty())
        assertEquals("You have no notifications.", NotificationIntel.summary(listOf(item()), NotificationPrefs()))
    }

    @Test
    fun ongoingGroupSummaryAndIgnoredAppsAreSkipped() {
        assertTrue(NotificationIntel.allowed(item(), on))
        assertFalse(NotificationIntel.allowed(item(ongoing = true), on))
        assertFalse(NotificationIntel.allowed(item(summary = true), on))
        assertFalse(NotificationIntel.allowed(item(title = "", text = " "), on))
        assertFalse(NotificationIntel.allowed(item(), on.copy(ignoredApps = setOf("com.whatsapp"))))
        assertFalse(NotificationIntel.allowed(item(), on.copy(selectedApps = setOf("com.google.android.gm"))))
        assertTrue(NotificationIntel.allowed(item(), on.copy(selectedApps = setOf("com.whatsapp"))))
    }

    @Test
    fun oneTimeCodesAndBankDetailsAreNeverSpoken() {
        val otp = item(title = "VM-HDFCBK", text = "123456 is your OTP for the transaction", message = false, app = "Messages")
        assertTrue(NotificationIntel.isSensitive(otp))
        val spoken = NotificationIntel.speak(otp, on)
        assertFalse(spoken.contains("123456"))
        assertTrue(spoken.contains("won't read"))
        assertTrue(NotificationIntel.isSensitive(item(text = "Rs 500 debited from a/c XX1234")))
        assertTrue(NotificationIntel.isSensitive(item(text = "Your verification code: 4821")))
        assertFalse(NotificationIntel.isSensitive(item()))
        // The user can choose to hear them.
        assertTrue(NotificationIntel.speak(otp, on.copy(hideSensitive = false)).contains("123456"))
    }

    @Test
    fun spokenFormNamesSenderAndApp() {
        assertEquals("Priya on WhatsApp: Are we still on for lunch?", NotificationIntel.speak(item(), on))
        assertEquals("Gmail, Invoice: Your receipt is ready.", NotificationIntel.speak(item(app = "Gmail", title = "Invoice", text = "Your receipt  is\nready", message = false), on))
    }

    @Test
    fun quietHoursWrapAroundMidnight() {
        val prefs = on.copy(quietStart = 22 * 60, quietEnd = 7 * 60)
        assertTrue(NotificationIntel.inQuietHours(prefs, 23 * 60))
        assertTrue(NotificationIntel.inQuietHours(prefs, 3 * 60))
        assertFalse(NotificationIntel.inQuietHours(prefs, 12 * 60))
        assertFalse(NotificationIntel.inQuietHours(on.copy(quietStart = 60, quietEnd = 60), 60))
        val day = on.copy(quietStart = 9 * 60, quietEnd = 17 * 60)
        assertTrue(NotificationIntel.inQuietHours(day, 10 * 60))
        assertFalse(NotificationIntel.inQuietHours(day, 17 * 60))
    }

    @Test
    fun announcementsNeedEveryOptIn() {
        val urgent = item(text = "Call me, it's urgent")
        val noon = 12 * 60
        assertFalse(NotificationIntel.shouldAnnounce(urgent, on, noon))
        val announce = on.copy(announceImportant = true, quietStart = 22 * 60, quietEnd = 7 * 60)
        assertTrue(NotificationIntel.shouldAnnounce(urgent, announce, noon))
        assertFalse(NotificationIntel.shouldAnnounce(urgent, announce, 23 * 60))
        assertFalse(NotificationIntel.shouldAnnounce(urgent, announce.copy(paused = true), noon))
        assertFalse(NotificationIntel.shouldAnnounce(item(), announce, noon))
        assertFalse(NotificationIntel.shouldAnnounce(item(text = "urgent: your OTP is 998877", high = true), announce, noon))
    }

    @Test
    fun summaryGroupsByAppAndCountsImportant() {
        val items = listOf(
            item(key = "1", at = 3),
            item(key = "2", title = "Arun", text = "urgent, call me", at = 2),
            item(key = "3", pkg = "com.google.android.gm", app = "Gmail", title = "Newsletter", text = "Weekly digest", message = false, at = 1),
        )
        assertEquals("You have 3 notifications: 2 from WhatsApp (Priya, Arun), 1 from Gmail. 1 looks important.", NotificationIntel.summary(items, on))
    }

    @Test
    fun latestMessageCanBeNarrowedBySenderOrApp() {
        val items = listOf(
            item(key = "old", title = "Priya", at = 1),
            item(key = "new", title = "Arun", text = "Running late", at = 5),
            item(key = "mail", pkg = "com.google.android.gm", app = "Gmail", title = "Bank", message = false, at = 9),
        )
        assertEquals("new", NotificationIntel.latestMessage(items, on)?.key)
        assertEquals("old", NotificationIntel.latestMessage(items, on, from = "priya")?.key)
        assertEquals("new", NotificationIntel.latestMessage(items, on, app = "whats app")?.key)
        assertNull(NotificationIntel.latestMessage(items, on, from = "Zed"))
    }

    @Test
    fun briefingListsTodayOverdueCalendarAndNotifications() {
        val time = FakeTime()
        val now = time.now()
        val due = Task(id = 1, title = "Submit the form", createdAt = 0, dueAt = time.toMillis(now.withHour(15).withMinute(0)))
        val late = Task(id = 2, title = "Pay rent", createdAt = 0, dueAt = time.toMillis(now.minusDays(2)))
        val text = BriefingComposer.compose(
            BriefingData("Sharvesh", now, listOf(due), listOf(late), listOf("Standup at 11 AM"), "You have 2 notifications: 2 from Gmail.", 80),
            BriefingOptions(),
            time,
        )
        assertEquals(
            "Good morning, Sharvesh. It's Friday, 13 November. You have 1 task today: Submit the form at 3 PM. " +
                "1 task is overdue, including Pay rent. On your calendar: Standup at 11 AM. You have 2 notifications: 2 from Gmail.",
            text,
        )
    }

    @Test
    fun briefingRespectsOptionsAndSkipsUnreadableSections() {
        val time = FakeTime()
        val data = BriefingData(null, time.now().withHour(19), emptyList(), emptyList(), null, null, 42)
        assertEquals("Good evening. It's Friday, 13 November. Nothing is due today. Battery is at 42 percent.", BriefingComposer.compose(data, BriefingOptions(battery = true), time))
        assertEquals("Good evening. It's Friday, 13 November.", BriefingComposer.compose(data, BriefingOptions(tasks = false), time))
        val clear = data.copy(events = emptyList())
        assertTrue(BriefingComposer.compose(clear, BriefingOptions(), time).endsWith("Your calendar is clear."))
    }
}
