package com.lumi.core

import com.lumi.core.ai.ToolCall
import com.lumi.core.nlu.LocalIntent
import com.lumi.core.nlu.LocalIntentParser
import com.lumi.core.nlu.SmallTalk
import com.lumi.core.tools.Args
import com.lumi.core.tools.Directive
import com.lumi.core.tools.ToolIds
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalIntentParserTest {
    private val parser = LocalIntentParser(FakeTime())

    private fun call(text: String): ToolCall {
        val intent = parser.parse(text)
        assertTrue("\"$text\" should map to a tool call but was $intent", intent is LocalIntent.Calls)
        return (intent as LocalIntent.Calls).calls.single()
    }

    private fun expect(text: String, tool: String, vararg args: Pair<String, String?>) {
        val c = call(text)
        assertEquals("tool for \"$text\"", tool, c.name)
        for ((k, v) in args) assertEquals("arg $k for \"$text\"", v, Args(c.args).string(k))
    }

    @Test fun reminderWithDateBeforeTheTask() =
        expect("Remind me tomorrow to complete my assignment", ToolIds.CREATE_TASK, "title" to "complete my assignment", "when" to "2026-11-14T09:00")

    @Test fun reminderWithTimeAfterTheTask() =
        expect("remind me to call mom at 7 pm", ToolIds.CREATE_TASK, "title" to "call mom", "when" to "2026-11-13T19:00")

    @Test fun wakeWordAndPolitenessAreIgnored() =
        expect("Hey Lumi, could you please remind me in 20 minutes to check the oven?", ToolIds.CREATE_TASK, "title" to "check the oven", "when" to "2026-11-13T10:20")

    @Test fun recurringReminder() =
        expect("remind me every day at 8 am to take vitamins", ToolIds.CREATE_TASK, "title" to "take vitamins", "when" to "2026-11-14T08:00", "recurrence" to "FREQ=DAILY;INTERVAL=1")

    @Test fun locationReminders() {
        expect("remind me to buy milk when I reach home", ToolIds.CREATE_TASK, "title" to "buy milk", "place" to "home", "on_leave" to "false")
        expect("remind me to lock the door when I leave home", ToolIds.CREATE_TASK, "title" to "lock the door", "place" to "home", "on_leave" to "true")
        expect("remind me about this when I reach college", ToolIds.ADD_REMINDER, "place" to "college", "on_leave" to "false")
    }

    @Test fun plainTask() =
        expect("add a task to finish the report by friday", ToolIds.CREATE_TASK, "title" to "finish the report", "when" to "2026-11-20T09:00")

    @Test fun reminderWithNothingElseAsksLater() {
        val c = call("remind me")
        assertEquals(ToolIds.CREATE_TASK, c.name)
        assertNull(Args(c.args).string("title"))
    }

    @Test fun listing() {
        expect("what are my tasks today", ToolIds.LIST_TASKS, "filter" to "today")
        expect("what do i have tomorrow", ToolIds.LIST_TASKS, "filter" to "tomorrow")
        expect("show my overdue tasks", ToolIds.LIST_TASKS, "filter" to "overdue")
        expect("what's pending", ToolIds.LIST_TASKS, "filter" to "pending")
        expect("what are my important tasks", ToolIds.LIST_TASKS, "filter" to "important")
        expect("show completed tasks", ToolIds.LIST_TASKS, "filter" to "completed")
        expect("what reminders do i have near home", ToolIds.LIST_LOCATION_REMINDERS)
    }

    @Test fun completingAndDeleting() {
        expect("mark the assignment as done", ToolIds.COMPLETE_TASK, "query" to "assignment")
        expect("I finished the report", ToolIds.COMPLETE_TASK, "query" to "report")
        expect("delete the milk reminder", ToolIds.DELETE_TASK, "query" to "milk")
        expect("delete all completed tasks", ToolIds.DELETE_COMPLETED)
    }

    @Test fun postponing() {
        expect("postpone the assignment by one hour", ToolIds.POSTPONE_TASK, "query" to "assignment", "minutes" to "60")
        expect("snooze that for 10 minutes", ToolIds.POSTPONE_TASK, "minutes" to "10")
        expect("move my assignment reminder to tomorrow 5 pm", ToolIds.POSTPONE_TASK, "query" to "assignment")
    }

    @Test fun remindUntilDone() {
        expect("keep reminding me every 10 minutes until i finish the assignment", ToolIds.SET_NAG, "query" to "assignment", "minutes" to "10")
        expect("remind me again if I haven't completed this", ToolIds.SET_NAG)
    }

    @Test fun calling() {
        expect("call dad", ToolIds.CALL, "contact" to "dad", "via" to "phone")
        expect("call mom on whatsapp", ToolIds.CALL, "contact" to "mom", "via" to "whatsapp")
        expect("call back the last caller", ToolIds.CALL, "last_caller" to "true")
        expect("call my last missed call", ToolIds.CALL, "last_caller" to "true")
        expect("call the person who called me last", ToolIds.CALL, "last_caller" to "true")
        expect("call 98765 43210", ToolIds.CALL, "number" to "98765 43210")
    }

    @Test fun messaging() {
        expect("read my latest message", ToolIds.READ_MESSAGES)
        expect("what did alex say", ToolIds.READ_MESSAGES, "from" to "alex")
        expect("reply saying i'll call later", ToolIds.DRAFT_REPLY, "text" to "i'll call later")
        expect("send a message to alex saying i am running late", ToolIds.DRAFT_MESSAGE, "to" to "alex", "text" to "i am running late")
        expect("whatsapp alex saying hello there", ToolIds.DRAFT_MESSAGE, "to" to "alex", "app" to "whatsapp")
        expect("write an email to john@x.com saying the report is ready", ToolIds.DRAFT_EMAIL, "to" to "john@x.com", "body" to "the report is ready")
    }

    @Test fun notifications() {
        expect("read my notifications", ToolIds.NOTIFICATIONS, "mode" to "read")
        expect("summarize my notifications", ToolIds.NOTIFICATIONS, "mode" to "summary")
        expect("any important notifications", ToolIds.NOTIFICATIONS, "mode" to "important")
        expect("ignore notifications from this app", ToolIds.IGNORE_APP, "app" to "current")
        expect("pause notifications", ToolIds.PAUSE_NOTIFICATIONS, "paused" to "true")
    }

    @Test fun mediaAndApps() {
        expect("open spotify", ToolIds.OPEN_APP, "name" to "spotify")
        expect("pause the music", ToolIds.MEDIA_CONTROL, "action" to "pause")
        expect("next song", ToolIds.MEDIA_CONTROL, "action" to "next")
        expect("previous track", ToolIds.MEDIA_CONTROL, "action" to "previous")
        expect("volume up", ToolIds.SET_VOLUME, "direction" to "up")
        expect("set volume to 50 percent", ToolIds.SET_VOLUME, "percent" to "50")
        expect("play my workout playlist", ToolIds.PLAY_MUSIC, "query" to "my workout playlist")
        expect("play believer on spotify", ToolIds.PLAY_MUSIC, "query" to "believer", "app" to "spotify")
        expect("what song is this", ToolIds.NOW_PLAYING)
    }

    @Test fun utilities() {
        expect("navigate to the airport", ToolIds.NAVIGATE, "destination" to "the airport")
        expect("take me home", ToolIds.NAVIGATE, "destination" to "home")
        expect("set an alarm for 6 30 am", ToolIds.SET_ALARM, "hour" to "6", "minute" to "30")
        expect("set an alarm for 6:30 pm", ToolIds.SET_ALARM, "hour" to "18", "minute" to "30")
        expect("wake me up at 7", ToolIds.SET_ALARM, "hour" to "7", "minute" to "0")
        expect("set a timer for 5 minutes", ToolIds.SET_TIMER, "seconds" to "300")
        expect("search for kotlin coroutines", ToolIds.WEB_SEARCH, "query" to "kotlin coroutines")
        expect("what's my battery", ToolIds.BATTERY)
        expect("what's on my calendar today", ToolIds.CALENDAR, "day" to "today")
        expect("good morning", ToolIds.BRIEFING)
    }

    @Test fun screen() {
        expect("what's on my screen", ToolIds.READ_SCREEN)
        expect("explain this", ToolIds.READ_SCREEN, "mode" to "explain")
        expect("scroll down", ToolIds.SCREEN_ACTION, "action" to "scroll_down")
        expect("click the second button", ToolIds.SCREEN_ACTION, "action" to "click", "ordinal" to "second")
        expect("tap on sign in", ToolIds.SCREEN_ACTION, "action" to "click", "target" to "sign in")
    }

    @Test fun memory() {
        expect("remember that my locker code is 4521", ToolIds.REMEMBER, "content" to "my locker code is 4521")
        expect("what's my locker code", ToolIds.RECALL, "query" to "locker code")
        expect("forget my locker code", ToolIds.FORGET, "query" to "locker code")
        expect("my name is Sam", ToolIds.SET_PREFERENCE, "key" to "preferred_name", "value" to "Sam")
        expect("mom is amma in my contacts", ToolIds.SET_ALIAS, "alias" to "mom", "name" to "amma")
        expect("remember this place as gym", ToolIds.SAVE_PLACE, "name" to "gym")
        expect("stop learning about me", ToolIds.SET_LEARNING, "enabled" to "false")
    }

    @Test fun rememberToDoSomethingIsAReminderNotAMemory() =
        expect("remember to call the bank tomorrow", ToolIds.CREATE_TASK, "title" to "call the bank")

    @Test fun controlAndSmallTalk() {
        assertEquals(Directive.StopSpeaking, (parser.parse("stop") as LocalIntent.Control).directive)
        assertEquals(Directive.Repeat, (parser.parse("repeat that") as LocalIntent.Control).directive)
        assertEquals(Directive.SpeechRate(1), (parser.parse("speak faster") as LocalIntent.Control).directive)
        assertEquals(Directive.SpeechRate(-1), (parser.parse("slow down") as LocalIntent.Control).directive)
        assertEquals(Directive.Dismiss, (parser.parse("never mind") as LocalIntent.Control).directive)
        assertEquals(Directive.OpenScreen("tasks"), (parser.parse("open tasks") as LocalIntent.Control).directive)
        assertEquals(SmallTalk.TIME, (parser.parse("what time is it") as LocalIntent.Chat).kind)
        assertEquals(SmallTalk.GREETING, (parser.parse("hello") as LocalIntent.Chat).kind)
        assertEquals(LocalIntent.StartOnboarding, parser.parse("let's get to know each other"))
        assertEquals(LocalIntent.StartOnboarding, parser.parse("set up lumi"))
    }

    @Test fun openQuestionsAreLeftToTheAi() {
        assertNull(parser.parse("what is the capital of france"))
        assertNull(parser.parse("tell me a joke"))
        assertNull(parser.parse("why is the sky blue"))
        assertNull(parser.parse(""))
    }

    @Test fun yesAndNo() {
        for (y in listOf("yes", "yeah", "sure", "go ahead", "do it", "yes please", "Yep.")) assertTrue(y, parser.isYes(y))
        for (n in listOf("no", "nope", "cancel", "don't", "no thanks")) assertTrue(n, parser.isNo(n))
        assertTrue(!parser.isYes("yesterday's report"))
        assertTrue(!parser.isNo("note this down"))
    }

    @Test fun draftCommands() {
        fun tool(text: String) = (parser.parseDraftCommand(text) as? LocalIntent.Calls)?.calls?.single()
        assertEquals(ToolIds.SEND_DRAFT, tool("send it")!!.name)
        assertEquals(ToolIds.SEND_DRAFT, tool("yes")!!.name)
        assertEquals(ToolIds.DISCARD_DRAFT, tool("cancel")!!.name)
        assertEquals(ToolIds.READ_DRAFT, tool("read it back")!!.name)
        val edit = tool("change it to I'll be late")!!
        assertEquals(ToolIds.EDIT_DRAFT, edit.name)
        assertEquals("replace", Args(edit.args).string("mode"))
        assertEquals("append", Args(tool("add see you soon")!!.args).string("mode"))
        assertEquals(ToolIds.REVISE_DRAFT, tool("make it more polite")!!.name)
        assertNull(tool("what time is it"))
        assertNotNull(tool("type it"))
    }
}
