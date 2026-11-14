package com.lumi.core

import com.lumi.core.ai.AiErrorKind
import com.lumi.core.ai.AiResult
import com.lumi.core.ai.Role
import com.lumi.core.ai.ToolCall
import com.lumi.core.model.MemoryCategory
import com.lumi.core.memory.MemoryService
import com.lumi.core.tasks.ScheduleOutcome
import com.lumi.core.tools.Args
import com.lumi.core.tools.Directive
import com.lumi.core.tools.Draft
import com.lumi.core.tools.ReplyTarget
import com.lumi.core.tools.ToolIds
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDateTime

/** End-to-end conversations through the orchestrator with in-memory persistence. */
class ConversationTest {
    private val h = Harness()

    private fun contains(text: String, part: String) = assertTrue("expected \"$part\" in: $text", text.contains(part, ignoreCase = true))

    @Test fun remindMeTomorrowCreatesTaskAndLinkedReminder() {
        val reply = h.say("Remind me tomorrow to complete my assignment")
        assertTrue(reply.text, reply.success)
        contains(reply.text, "complete my assignment")
        contains(reply.text, "tomorrow")
        val task = h.repo.tasks.values.single()
        assertEquals("Complete my assignment", task.title)
        val reminder = h.repo.reminders.values.single()
        assertEquals(task.id, reminder.taskId)
        assertEquals(h.time.millis(LocalDateTime.of(2026, 11, 14, 9, 0)), h.scheduler.alarms[reminder.id])
    }

    @Test fun neverClaimsAReminderWasSetWhenSchedulingFailed() {
        h.scheduler.timeOutcome = ScheduleOutcome.Failed("the system refused the alarm")
        val reply = h.say("remind me to call mom at 7 pm")
        assertFalse(reply.text, reply.success)
        assertTrue(h.scheduler.alarms.isEmpty())
        assertFalse(reply.text, Regex("\\bI'll remind you\\b", RegexOption.IGNORE_CASE).containsMatchIn(reply.text))
    }

    @Test fun inexactAlarmIsDisclosed() {
        h.scheduler.timeOutcome = ScheduleOutcome.Inexact("exact alarms are not allowed")
        val reply = h.say("remind me to call mom at 7 pm")
        assertTrue(reply.success)
        assertTrue(reply.text, Regex("exact|few minutes|late", RegexOption.IGNORE_CASE).containsMatchIn(reply.text))
    }

    @Test fun missingTimeIsAskedForAndThenUsed() {
        val first = h.say("remind me to water the plants")
        assertTrue(first.text, first.expectReply)
        assertTrue(h.repo.tasks.isEmpty())
        val second = h.say("tomorrow at 6 pm")
        assertTrue(second.text, second.success)
        assertEquals(h.time.millis(LocalDateTime.of(2026, 11, 14, 18, 0)), h.scheduler.alarms.values.single())
        assertEquals("Water the plants", h.repo.tasks.values.single().title)
    }

    @Test fun aReminderWithoutATimeCanBecomeAPlainTask() {
        h.say("remind me to water the plants")
        val reply = h.say("no time")
        assertTrue(reply.text, reply.success)
        assertEquals(1, h.repo.tasks.size)
        assertTrue(h.scheduler.alarms.isEmpty())
        contains(reply.text, "no due date")
    }

    @Test fun plainTasksDoNotAskForATime() {
        val reply = h.say("add a task to clean my desk")
        assertFalse(reply.text, reply.expectReply)
        assertEquals("Clean my desk", h.repo.tasks.values.single().title)
    }

    @Test fun remindMeAboutThisWhenIReachCollegeAttachesAPlaceToTheLastTask() {
        h.places.places += com.lumi.core.model.Place(1, "college", 8.5, 76.9)
        h.say("remind me to return the library book at 4 pm")
        val reply = h.say("remind me about this when I reach college")
        assertTrue(reply.text, reply.success)
        assertEquals(1, h.repo.tasks.size)
        assertEquals(2, h.repo.reminders.size)
        assertEquals(1, h.scheduler.geofences.size)
    }

    @Test fun missingTitleIsAskedFor() {
        val first = h.say("remind me")
        assertTrue(first.expectReply)
        val second = h.say("call the dentist at 4 pm")
        assertTrue(second.text, second.success)
        assertEquals("Call the dentist", h.repo.tasks.values.single().title)
        assertEquals(h.time.millis(LocalDateTime.of(2026, 11, 13, 16, 0)), h.scheduler.alarms.values.single())
    }

    @Test fun listCompletePostponeByVoice() {
        h.say("remind me tomorrow to complete my assignment")
        h.say("remind me to buy milk at 6 pm")
        val list = h.say("what are my tasks today")
        contains(list.text, "buy milk")
        assertFalse(list.text.contains("assignment", ignoreCase = true))

        val postponed = h.say("postpone the assignment by one hour")
        assertTrue(postponed.text, postponed.success)
        val assignment = h.repo.tasks.values.first { it.title.contains("assignment") }
        assertEquals(h.time.millis(LocalDateTime.of(2026, 11, 14, 10, 0)), assignment.dueAt)

        val done = h.say("mark the assignment as done")
        assertTrue(done.text, done.success)
        assertTrue(h.repo.tasks[assignment.id]!!.completed)
        assertEquals(1, h.scheduler.alarms.size)
    }

    @Test fun completingSomethingThatDoesNotExistFailsHonestly() {
        val reply = h.say("mark the dentist appointment as done")
        assertFalse(reply.success)
    }

    @Test fun deletingATaskNeedsConfirmation() {
        h.say("remind me to buy milk at 6 pm")
        val ask = h.say("delete the milk reminder")
        assertTrue(ask.text, ask.expectReply)
        assertEquals(1, h.repo.tasks.size)
        val no = h.say("no")
        assertEquals(1, h.repo.tasks.size)
        contains(no.text, "cancel")
        h.say("delete the milk reminder")
        val yes = h.say("yes")
        assertTrue(yes.text, yes.success)
        assertTrue(h.repo.tasks.isEmpty())
        assertTrue(h.scheduler.alarms.isEmpty())
    }

    @Test fun locationReminderWithUnknownPlaceIsHonestAboutNotBeingArmed() {
        val reply = h.say("remind me to buy milk when I reach home")
        contains(reply.text, "home")
        assertTrue(h.scheduler.geofences.isEmpty())
        assertEquals(1, h.repo.tasks.size)
        assertFalse(reply.text, reply.text.contains("I'll remind you when you", ignoreCase = true) && !reply.text.contains("once", ignoreCase = true) && !reply.text.contains("don't know", ignoreCase = true))
    }

    @Test fun callsAreConfirmedFirst() {
        val ask = h.say("call dad")
        assertTrue(ask.expectReply)
        assertTrue(h.call.invocations.isEmpty())
        val done = h.say("yes")
        assertEquals("dad", h.call.invocations.single().string("contact"))
        assertTrue(done.success)
    }

    @Test fun callConfirmationCanBeTurnedOff() {
        h.confirmCalls = false
        h.say("call dad")
        assertEquals(1, h.call.invocations.size)
    }

    @Test fun missingPermissionIsExplainedAndNothingRuns() {
        h.permissions.denied += "android.permission.CALL_PHONE"
        val reply = h.say("call dad")
        assertFalse(reply.success)
        contains(reply.text, "permission")
        assertEquals("permissions", reply.suggestScreen)
        assertTrue(h.call.invocations.isEmpty())
    }

    @Test fun lockedPhoneBlocksProtectedActionsUntilUnlock() {
        val reply = h.say("call dad", locked = true)
        assertEquals(Directive.RequestUnlock, reply.directive)
        assertTrue(h.call.invocations.isEmpty())
        val resumed = runBlocking { h.orchestrator.resumeAfterUnlock() }!!
        assertTrue(resumed.text, resumed.expectReply) // still asks for confirmation
        h.say("yes")
        assertEquals(1, h.call.invocations.size)
    }

    @Test fun lockedPhoneStillAllowsHarmlessActions() {
        val reply = h.say("pause the music", locked = true)
        assertTrue(reply.success)
        assertEquals(1, h.media.invocations.size)
    }

    @Test fun lockedPhoneDoesNotRevealTasksOrMemories() {
        h.say("remember that my locker code is 4521")
        h.say("remind me to buy a gift at 6 pm")
        val tasks = h.say("what are my tasks today", locked = true)
        assertFalse(tasks.text, tasks.text.contains("gift"))
        val memory = h.say("what's my locker code", locked = true)
        assertFalse(memory.text, memory.text.contains("4521"))
    }

    @Test fun dictationIsReadBackEditedAndOnlySentAfterConfirmation() {
        val draft = h.say("send a message to alex saying i am running late")
        assertTrue(draft.expectReply)
        contains(draft.text, "I am running late")
        assertTrue(h.messaging.sent.isEmpty())

        val edited = h.say("change it to I'll be there in ten minutes")
        contains(edited.text, "I'll be there in ten minutes")
        assertTrue(h.messaging.sent.isEmpty())

        val polite = h.say("make it more polite")
        assertTrue(polite.text, polite.expectReply)
        assertTrue(h.messaging.sent.isEmpty())

        val sent = h.say("send it")
        assertTrue(sent.success)
        val d = h.messaging.sent.single()
        assertEquals(Draft.Kind.MESSAGE, d.kind)
        assertEquals("+911234567890", d.recipientAddress)
        assertNull(h.session.draft)
    }

    @Test fun draftCanBeDiscarded() {
        h.say("text mom that i'll be home by 8")
        val reply = h.say("cancel")
        contains(reply.text, "Nothing was sent")
        assertTrue(h.messaging.sent.isEmpty())
        assertNull(h.session.draft)
    }

    @Test fun replyNeedsAMessageToReplyTo() {
        val none = h.say("reply saying i'll call later")
        assertFalse(none.success)
        h.messaging.target = ReplyTarget("key1", "Alex", "WhatsApp")
        val draft = h.say("reply saying i'll call later")
        contains(draft.text, "Alex")
        h.say("yes")
        assertEquals("key1", h.messaging.sent.single().replyKey)
    }

    @Test fun unknownRecipientIsReported() {
        val reply = h.say("send a message to zorblax saying hi")
        assertFalse(reply.success)
        assertNull(h.session.draft)
    }

    @Test fun sendItWithoutADraftDoesNothing() {
        val reply = h.say("send it")
        assertFalse(reply.success)
        assertTrue(h.messaging.sent.isEmpty())
    }

    @Test fun memoryRoundTripAndForgetWithConfirmation() {
        assertTrue(h.say("remember that my locker code is 4521").success)
        contains(h.say("what's my locker code").text, "4521")
        val ask = h.say("forget my locker code")
        assertTrue(ask.text, ask.expectReply)
        assertEquals(1, h.memoryStore.items.size)
        h.say("yes")
        assertTrue(h.memoryStore.items.isEmpty())
    }

    @Test fun nameIsUsedInGreeting() {
        h.say("my name is Sam")
        contains(h.say("hello").text, "Sam")
    }

    @Test fun repeatSaysTheLastThingAgain() {
        val first = h.say("who are you")
        val again = h.say("repeat that")
        assertEquals(first.text, again.text)
    }

    @Test fun onboardingCanBeSkippedAndStopped() {
        val start = h.say("let's get to know each other")
        assertTrue(start.expectReply)
        contains(start.text, "call you")
        val second = h.say("Sam")
        assertTrue(second.expectReply)
        val third = h.say("skip")
        assertTrue(third.expectReply)
        val stop = h.say("stop")
        assertFalse(stop.expectReply)
        assertEquals("Sam", runBlocking { h.memory.get(MemoryCategory.PREFERENCE, MemoryService.KEY_NAME) })
        // Normal commands work again afterwards.
        assertTrue(h.say("remind me to stretch at 5 pm").success)
    }

    @Test fun repeatedRemindersLeadToAnOfferThatIsOnlyAppliedIfApproved() {
        h.say("remind me to water the plants at 6 pm")
        h.say("remind me to water the plants tomorrow at 6 pm")
        val third = h.say("remind me to water the plants on monday at 6 pm")
        assertTrue(third.text, third.expectReply)
        assertEquals(3, h.repo.tasks.size)
        val declined = h.say("no")
        assertFalse(declined.expectReply)
        assertEquals(3, h.repo.tasks.size)
        // Declined patterns are never offered again.
        val fourth = h.say("remind me to water the plants on tuesday at 6 pm")
        assertFalse(fourth.text, fourth.expectReply)
    }

    @Test fun approvedPatternBecomesARecurringReminder() {
        h.say("remind me to water the plants at 6 pm")
        h.say("remind me to water the plants tomorrow at 6 pm")
        h.say("remind me to water the plants on monday at 6 pm")
        val how = h.say("yes")
        assertTrue(how.text, how.expectReply)
        val done = h.say("every day at 6 pm")
        assertTrue(done.text, done.success)
        assertEquals(1, h.repo.tasks.values.count { it.recurrence != null })
    }

    @Test fun learningOffDisablesOffers() {
        h.policy.learning = false
        repeat(4) { h.say("remind me to water the plants at ${it + 5} pm") }
        assertTrue(h.patterns.map.isEmpty())
    }

    // ------------------------------------------------------------------ AI path

    @Test fun withoutAnAiProviderOpenQuestionsAreDeclinedHonestly() {
        val reply = h.say("what is the capital of france")
        assertFalse(reply.success)
        assertEquals("settings", reply.suggestScreen)
    }

    @Test fun commonCommandsNeverTouchTheAi() {
        val ai = FakeAi().also { h.ai = it }
        h.say("remind me to call mom at 7 pm")
        h.say("pause the music")
        h.say("what are my tasks today")
        assertTrue(ai.requests.isEmpty())
    }

    @Test fun aiAnswerIsSpokenWithoutMarkdown() {
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Ok("**Paris** is the capital of France.", emptyList())
        assertEquals("Paris is the capital of France.", h.say("what is the capital of france").text)
        val request = ai.requests.single()
        assertEquals(Role.SYSTEM, request.first().role)
        assertEquals("what is the capital of france", request.last().content)
    }

    @Test fun aiToolCallRunsThroughTheSameSafetyChecks() {
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Ok("", listOf(ToolCall("c1", ToolIds.CALL, Args.of("contact" to "dad"))))
        val ask = h.say("ring up my father would you")
        assertTrue(ask.expectReply)
        assertTrue(h.call.invocations.isEmpty())
        h.say("yes")
        assertEquals(1, h.call.invocations.size)
    }

    @Test fun aiCreatedReminderUsesTheVerifiedToolWording() {
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Ok("", listOf(ToolCall("c1", ToolIds.CREATE_TASK, Args.of("title" to "revise chemistry", "when" to "tomorrow 5pm"))))
        val reply = h.say("i should probably revise chemistry tomorrow at five in the evening, nudge me")
        assertTrue(reply.text, reply.success)
        assertEquals(h.time.millis(LocalDateTime.of(2026, 11, 14, 17, 0)), h.scheduler.alarms.values.single())
        assertEquals(1, ai.requests.size) // no second round trip needed
    }

    @Test fun aiCannotSendMessages() {
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Ok("", emptyList())
        h.say("tell me something interesting")
        assertTrue(ai.toolsSeen.isNotEmpty())
        assertTrue(ai.toolsSeen.none { it.name == ToolIds.SEND_DRAFT })
    }

    @Test fun aiSeesQueryResultsButNotPrivateContent() {
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Ok("", listOf(ToolCall("c1", ToolIds.READ_SCREEN, Args.of("mode" to "explain"))))
        ai.queue += AiResult.Ok("That page could not be found; the link is probably broken.", emptyList())
        val reply = h.say("why am i seeing this weird thing")
        contains(reply.text, "link is probably broken")
        assertTrue(ai.requests[1].any { it.role == Role.TOOL && it.content.contains("404") })

        val ai2 = FakeAi().also { h.ai = it }
        ai2.queue += AiResult.Ok("", listOf(ToolCall("c2", ToolIds.NOTIFICATIONS, Args.of("mode" to "read"))))
        val private = h.say("anything new from people")
        contains(private.text, "4321")
        assertTrue(ai2.requests.flatten().none { it.content.contains("4321") })
        assertTrue(h.history.turns.none { it.text.contains("4321") })
    }

    @Test fun aiFailuresAreExplained() {
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Error(AiErrorKind.NETWORK, "offline")
        val offline = h.say("what is the capital of france")
        assertFalse(offline.success)
        contains(offline.text, "couldn't reach")
        ai.queue += AiResult.Error(AiErrorKind.AUTH, "bad key")
        assertEquals("settings", h.say("what is the capital of france").suggestScreen)
        // Offline basics keep working.
        assertTrue(h.say("remind me to call mom at 7 pm").success)
    }

    @Test fun aiUnknownToolIsReportedNotFaked() {
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Ok("", listOf(ToolCall("c1", "launch_rocket", Args.of())))
        assertFalse(h.say("ignite the rocket thrusters").success)
    }

    @Test fun lockedAiTurnGetsNoMemoriesOrHistory() {
        h.say("remember that my locker code is 4521")
        val ai = FakeAi().also { h.ai = it }
        ai.queue += AiResult.Ok("Sure.", emptyList())
        h.say("tell me something interesting", locked = true)
        assertTrue(ai.requests.single().none { it.content.contains("4521") })
        assertTrue(ai.toolsSeen.none { it.name == ToolIds.RECALL || it.name == ToolIds.CALL })
    }

    @Test fun emptyInputIsHandled() {
        assertFalse(h.say("   ").success)
        assertNotNull(h.say("thanks").text)
    }
}
