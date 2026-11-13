package com.lumi.core.agent

import com.lumi.core.ai.AiClient
import com.lumi.core.ai.AiErrorKind
import com.lumi.core.ai.AiResult
import com.lumi.core.ai.ChatMessage
import com.lumi.core.ai.Role
import com.lumi.core.ai.ToolCall
import com.lumi.core.learning.LearningEngine
import com.lumi.core.learning.OnboardingFlow
import com.lumi.core.learning.PatternOffer
import com.lumi.core.memory.HistoryStore
import com.lumi.core.memory.MemoryService
import com.lumi.core.model.MemoryCategory
import com.lumi.core.model.TimeSource
import com.lumi.core.model.now
import com.lumi.core.nlu.LocalIntent
import com.lumi.core.nlu.LocalIntentParser
import com.lumi.core.nlu.SmallTalk
import com.lumi.core.time.DateTimeParser
import com.lumi.core.time.TimeDefaults
import com.lumi.core.time.TimeFormat
import com.lumi.core.tools.Args
import com.lumi.core.tools.Confirmation
import com.lumi.core.tools.Directive
import com.lumi.core.tools.PermissionGate
import com.lumi.core.tools.Preview
import com.lumi.core.tools.SessionState
import com.lumi.core.tools.TextReviser
import com.lumi.core.tools.ToolIds
import com.lumi.core.tools.ToolRegistry
import com.lumi.core.tools.ToolResult
import com.lumi.core.tools.TurnEnv
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.time.format.TextStyle
import java.util.Locale

/** What the assistant surface should say and do after one user turn. */
data class Reply(
    val text: String,
    /** Keep the microphone open for the user's answer. */
    val expectReply: Boolean = false,
    val directive: Directive? = null,
    val success: Boolean = true,
    /** A Lumi screen that would help (shown as a shortcut in the compact UI). */
    val suggestScreen: String? = null,
)

/** User-controlled behaviour switches read on every turn. */
interface AssistantSettings {
    /** Whether an IF_ENABLED tool (e.g. calling) must be confirmed first. */
    fun confirmationEnabled(toolId: String): Boolean
    fun timeDefaults(): TimeDefaults = TimeDefaults()
}

/** Sink for the debug/action log. */
interface ActionLog {
    suspend fun record(tool: String, args: String, success: Boolean, message: String)
}

/**
 * The conversation engine. One turn flows through:
 * context -> local intent parser -> (AI planner) -> tool selection -> permission and safety
 * validation -> execution -> verification -> natural-language reply.
 *
 * Common requests are resolved on-device; the AI provider is only consulted for what the local
 * parser does not understand, and only ever through structured tool calls.
 */
class Orchestrator(
    private val registry: ToolRegistry,
    private val parser: LocalIntentParser,
    private val session: SessionState,
    private val memory: MemoryService,
    private val history: HistoryStore,
    private val learning: LearningEngine,
    private val onboarding: OnboardingFlow,
    private val ai: () -> AiClient?,
    private val permissions: PermissionGate,
    private val settings: AssistantSettings,
    private val log: ActionLog,
    private val time: TimeSource,
) {
    private sealed class Pending {
        data class Confirm(val call: ToolCall, val rest: List<ToolCall>) : Pending()
        data class Clarify(val call: ToolCall, val param: String, val rest: List<ToolCall>) : Pending()
        data class Offer(val offer: PatternOffer) : Pending()
        data class OfferSchedule(val offer: PatternOffer, val attempts: Int = 0) : Pending()
        data class Unlock(val calls: List<ToolCall>) : Pending()
    }

    private sealed class Step {
        data class Done(val result: ToolResult, val private: Boolean) : Step()
        data class Halt(val reply: Reply) : Step()
    }

    private val mutex = Mutex()
    private var pending: Pending? = null
    private var lastReply: String? = null

    /** True while Lumi is waiting for the user to answer a question it asked. */
    val awaitingAnswer: Boolean get() = pending != null || onboarding.active || session.draft != null

    /** Handles one utterance and returns what to say. Never throws for tool or network failures. */
    suspend fun handle(utterance: String, env: TurnEnv = TurnEnv()): Reply = mutex.withLock {
        val text = utterance.trim()
        if (text.isEmpty()) return@withLock Reply("I didn't catch that.", success = false)
        var privateReply = false
        val reply = try {
            route(text, env) { privateReply = true }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Reply("Sorry, something went wrong on my side: ${e.message ?: e.javaClass.simpleName}.", success = false)
        }
        if (reply.text.isNotBlank() && reply.directive != Directive.Repeat) lastReply = reply.text
        if (!env.deviceLocked) {
            history.append("user", text)
            if (reply.text.isNotBlank()) history.append("assistant", if (privateReply) "(read private content aloud)" else reply.text)
        }
        reply
    }

    /** Continues the action that was blocked by the lock screen, after the user unlocked. */
    suspend fun resumeAfterUnlock(env: TurnEnv = TurnEnv()): Reply? = mutex.withLock {
        val p = pending as? Pending.Unlock ?: return@withLock null
        pending = null
        val reply = runCalls(p.calls, env.copy(deviceLocked = false), confirmed = false) {}
        if (reply.text.isNotBlank()) lastReply = reply.text
        reply
    }

    /** Drops any half-finished question, e.g. when the assistant surface is dismissed. */
    fun resetConversation() {
        pending = null
        onboarding.cancel()
    }

    // ------------------------------------------------------------------ routing

    private suspend fun route(text: String, env: TurnEnv, markPrivate: () -> Unit): Reply {
        if (onboarding.active) {
            val progress = onboarding.answer(text)
            return Reply(progress.reply, expectReply = !progress.finished)
        }

        when (val p = pending) {
            null -> {}
            is Pending.Confirm -> {
                pending = null
                if (parser.isYes(text)) return runCalls(listOf(p.call) + p.rest, env, confirmed = true, markPrivate = markPrivate)
                if (parser.isNo(text)) return Reply("Okay, cancelled. Nothing was done.")
            }
            is Pending.Clarify -> {
                pending = null
                val noTime = p.call.name == ToolIds.CREATE_TASK && p.param == "when" &&
                    Regex("(?:no|without)(?: specific| particular| set)? (?:time|date|reminder|due date)|whenever|any ?time|no need|none|don't remind me|just (?:add|save|keep) it|no", RegexOption.IGNORE_CASE).matches(parser.normalise(text))
                if (noTime) {
                    val call = p.call.copy(args = Args(p.call.args).with("reminder", "false"))
                    return runCalls(listOf(call) + p.rest, env, confirmed = false, markPrivate = markPrivate)
                }
                if (parser.isNo(text)) return Reply("Okay, never mind.")
                return runCalls(listOf(fill(p.call, p.param, text)) + p.rest, env, confirmed = false, markPrivate = markPrivate)
            }
            is Pending.Offer -> {
                pending = null
                if (parser.isYes(text)) return acceptOffer(p.offer)
                if (parser.isNo(text)) {
                    learning.resolve(p.offer.key, approved = false)
                    return Reply("No problem, I won't ask again.")
                }
            }
            is Pending.OfferSchedule -> {
                pending = null
                if (parser.isNo(text)) {
                    learning.resolve(p.offer.key, approved = false)
                    return Reply("Okay, I'll leave it.")
                }
                return scheduleOffer(p, text, env)
            }
            is Pending.Unlock -> pending = null
        }

        if (session.draft != null) {
            val draftIntent = parser.parseDraftCommand(text)
            if (draftIntent is LocalIntent.Calls) return runCalls(draftIntent.calls, env, confirmed = false, markPrivate = markPrivate)
        }

        return when (val intent = parser.parse(text)) {
            is LocalIntent.Control -> control(intent)
            is LocalIntent.Chat -> smallTalk(intent.kind)
            LocalIntent.StartOnboarding ->
                if (env.deviceLocked) Reply("Let's do that once your phone is unlocked.", success = false)
                else onboarding.start().let { Reply(it.reply, expectReply = true) }
            is LocalIntent.Calls -> {
                // A personal look-up with nothing saved locally is better answered by the AI, when there is one.
                val only = intent.calls.singleOrNull()
                val query = only?.takeIf { it.name == ToolIds.RECALL }?.let { Args(it.args).string("query") }
                if (query != null && !env.deviceLocked && ai() != null && memory.search(query).isEmpty()) aiTurn(text, env, markPrivate)
                else runCalls(intent.calls, env, confirmed = false, markPrivate = markPrivate)
            }
            null ->
                if (Regex("(?:send|type|read) (?:it|that|the (?:message|draft|reply))(?: back| now)?", RegexOption.IGNORE_CASE).matches(parser.normalise(text))) {
                    Reply("There's no draft at the moment. Tell me who to message and what to say.", success = false)
                } else {
                    aiTurn(text, env, markPrivate)
                }
        }
    }

    /** Merges the user's answer into the call that was waiting for it. */
    private fun fill(call: ToolCall, param: String, answer: String): ToolCall {
        val value = parser.normalise(answer)
        if (call.name == ToolIds.CREATE_TASK && param == "title" && Args(call.args).string("when") == null && Args(call.args).string("place") == null) {
            // The answer may carry its own time: "call dad at seven".
            val reparsed = (parser.parse("remind me to $value") as? LocalIntent.Calls)?.calls?.singleOrNull()
            if (reparsed != null && reparsed.name == ToolIds.CREATE_TASK) {
                var args = reparsed.args
                Args(call.args).string("priority")?.let { args = Args(args).with("priority", it) }
                return call.copy(args = args)
            }
        }
        return call.copy(args = Args(call.args).with(param, value))
    }

    private fun control(intent: LocalIntent.Control): Reply = when (intent.directive) {
        Directive.Repeat -> Reply(lastReply ?: "I haven't said anything yet.", directive = Directive.Repeat)
        Directive.StopSpeaking -> Reply("", directive = Directive.StopSpeaking)
        Directive.Dismiss -> {
            pending = null
            Reply(intent.say, directive = Directive.Dismiss)
        }
        else -> Reply(intent.say, directive = intent.directive)
    }

    private suspend fun smallTalk(kind: SmallTalk): Reply {
        val name = memory.get(MemoryCategory.PREFERENCE, MemoryService.KEY_NAME)
        val now = time.now()
        return when (kind) {
            SmallTalk.GREETING -> Reply(if (name != null) "Hey $name. What can I do for you?" else "Hey there. What can I do for you?", expectReply = true)
            SmallTalk.THANKS -> Reply(listOf("Anytime.", "Happy to help.", "You got it.")[(now.second) % 3])
            SmallTalk.HOW_ARE_YOU -> Reply("Running smoothly, thanks for asking. What do you need?", expectReply = true)
            SmallTalk.IDENTITY -> Reply("I'm Lumi, your personal assistant. I keep track of your reminders and tasks, make calls, handle messages, control music and apps, and remember what matters to you.")
            SmallTalk.HELP -> Reply(
                "Try things like: remind me to submit my assignment tomorrow, what do I have today, call Dad, " +
                    "read my latest message, play my workout playlist, or remember that my locker code is on the blue card. " +
                    "For open-ended questions I use the AI provider you set up in settings.",
            )
            SmallTalk.TIME -> Reply("It's ${TimeFormat.time(now.toLocalTime().withSecond(0).withNano(0))}.")
            SmallTalk.DATE -> Reply(
                "It's ${now.dayOfWeek.getDisplayName(TextStyle.FULL, Locale.ENGLISH)}, ${now.dayOfMonth} " +
                    "${now.month.getDisplayName(TextStyle.FULL, Locale.ENGLISH)} ${now.year}.",
            )
        }
    }

    // ------------------------------------------------------------------ learning offers

    private suspend fun acceptOffer(offer: PatternOffer): Reply = when (offer.kind) {
        PatternOffer.Kind.RECURRING_REMINDER -> {
            pending = Pending.OfferSchedule(offer)
            Reply("Great. How often should I remind you? For example: every day at 8 AM, or every Monday.", expectReply = true)
        }
        PatternOffer.Kind.MUSIC_APP -> {
            memory.remember(MemoryCategory.APP_PREFERENCE, offer.subject, MemoryService.KEY_MUSIC_APP, source = "learned")
            learning.resolve(offer.key, approved = true)
            Reply("Done. I'll use ${offer.subject} for music from now on.")
        }
    }

    private suspend fun scheduleOffer(p: Pending.OfferSchedule, text: String, env: TurnEnv): Reply {
        val parsed = DateTimeParser.parse(text, time.now(), settings.timeDefaults())
        val rec = parsed.recurrence
        val at = parsed.dateTime
        if (rec == null || at == null) {
            if (p.attempts >= 1) return Reply("I still couldn't work out a schedule, so I've left it. You can say something like: remind me about ${p.offer.subject} every Monday.", success = false)
            pending = p.copy(attempts = p.attempts + 1)
            return Reply("I didn't catch a repeating schedule. Try something like: every weekday at 7 AM.", expectReply = true, success = false)
        }
        val call = ToolCall("local", ToolIds.CREATE_TASK, Args.of("title" to p.offer.subject, "when" to at.toString(), "recurrence" to rec.encode()))
        val reply = runCalls(listOf(call), env, confirmed = true) {}
        if (reply.success) {
            learning.resolve(p.offer.key, approved = true)
            memory.learn(MemoryCategory.PATTERN, "Recurring reminder: ${p.offer.subject}, ${rec.describe()}", key = p.offer.key)
        }
        return reply
    }

    // ------------------------------------------------------------------ tool execution

    private suspend fun runCalls(calls: List<ToolCall>, env: TurnEnv, confirmed: Boolean, markPrivate: () -> Unit): Reply {
        val spoken = mutableListOf<String>()
        var last: ToolResult? = null
        var offer: PatternOffer? = null
        for ((i, call) in calls.withIndex()) {
            when (val step = runOne(call, calls.drop(i + 1), env, confirmed && i == 0)) {
                is Step.Halt -> return if (spoken.isEmpty()) step.reply else step.reply.copy(text = (spoken + step.reply.text).joinToString(" "))
                is Step.Done -> {
                    val r = step.result
                    if (step.private) markPrivate()
                    last = r
                    if (r.message.isNotBlank()) spoken += r.message
                    if (r.offer != null) offer = r.offer
                    if (r.success && call.name == ToolIds.PLAY_MUSIC) offer = offer ?: musicOffer(call)
                    // Later steps usually depend on earlier ones; never continue past a failure.
                    if (!r.success || r.expectReply) break
                }
            }
        }
        val result = last ?: return Reply("I'm not sure what to do with that.", success = false)
        var text = spoken.joinToString(" ")
        var expect = result.expectReply
        val o = offer
        if (o != null && result.success && !expect) {
            pending = Pending.Offer(o)
            text = "$text ${o.question}"
            expect = true
        }
        return Reply(text, expect, result.directive, result.success, result.suggestScreen)
    }

    private suspend fun musicOffer(call: ToolCall): PatternOffer? {
        val app = Args(call.args).string("app") ?: return null
        val preferred = memory.preference(MemoryService.KEY_MUSIC_APP)
        return learning.observeMusicApp(app, alreadyPreferred = preferred.equals(app, ignoreCase = true))
    }

    private suspend fun runOne(call: ToolCall, rest: List<ToolCall>, env: TurnEnv, confirmed: Boolean): Step {
        val tool = registry.get(call.name)
            ?: return Step.Done(ToolResult.fail("I don't have a way to do that yet."), false)
        val spec = tool.spec
        if (env.deviceLocked && !spec.allowedWhenLocked) {
            pending = Pending.Unlock(listOf(call) + rest)
            return Step.Halt(Reply("Your phone is locked. Unlock it and I'll carry on.", directive = Directive.RequestUnlock, success = false))
        }
        val missing = permissions.missing(spec.permissions)
        if (missing.isNotEmpty()) {
            val names = missing.map { permissions.label(it) }.distinct().joinToString(" and ")
            return Step.Done(ToolResult.fail("I need the $names permission for that. You can grant it in Lumi's Permissions screen.", suggestScreen = "permissions"), false)
        }
        var args = Args(call.args)
        for (p in spec.params) {
            if (p.required && args.string(p.name) == null && p.prompt != null) {
                pending = Pending.Clarify(call, p.name, rest)
                return Step.Halt(Reply(p.prompt, expectReply = true))
            }
        }
        val needsConfirm = !confirmed && when (spec.confirmation) {
            Confirmation.NEVER -> false
            Confirmation.ALWAYS -> true
            Confirmation.IF_ENABLED -> settings.confirmationEnabled(spec.id)
        }
        val preview = try {
            tool.preview(args, env)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Preview.Fail("I couldn't prepare that: ${e.message ?: e.javaClass.simpleName}.")
        }
        when (preview) {
            Preview.Proceed -> if (needsConfirm) {
                pending = Pending.Confirm(call, rest)
                return Step.Halt(Reply("Should I go ahead and ${spec.id.replace('_', ' ')}?", expectReply = true))
            }
            is Preview.Ask -> {
                pending = Pending.Clarify(call, preview.param, rest)
                return Step.Halt(Reply(preview.prompt, expectReply = true))
            }
            is Preview.Fail -> return Step.Done(ToolResult.fail(preview.message, preview.suggestScreen), false)
            is Preview.Confirm -> {
                val resolved = if (preview.args != null) call.copy(args = preview.args) else call
                if (needsConfirm) {
                    pending = Pending.Confirm(resolved, rest)
                    return Step.Halt(Reply(preview.prompt, expectReply = true))
                }
                args = Args(resolved.args)
            }
        }
        val result = try {
            tool.execute(args, env)
        } catch (e: CancellationException) {
            throw e
        } catch (e: SecurityException) {
            ToolResult.fail("Android blocked that because a permission is missing.", suggestScreen = "permissions")
        } catch (e: Exception) {
            ToolResult.fail("That didn't work: ${e.message ?: e.javaClass.simpleName}.")
        }
        log.record(spec.id, if (spec.privateOutput) "(private)" else call.args.toString().take(300), result.success, if (spec.privateOutput) "(private content)" else result.message.take(300))
        return Step.Done(result, spec.privateOutput)
    }

    // ------------------------------------------------------------------ AI planner

    private suspend fun aiTurn(text: String, env: TurnEnv, markPrivate: () -> Unit): Reply {
        val client = ai() ?: return Reply(
            "I can't answer that on my own yet. I handle reminders, tasks, calls, messages, music and apps offline; " +
                "for open questions, add an AI provider in Lumi's settings.",
            success = false,
            suggestScreen = "settings",
        )
        val tools = registry.defs(lockedOnly = env.deviceLocked)
        val messages = mutableListOf(ChatMessage(Role.SYSTEM, systemPrompt(env)))
        if (!env.deviceLocked) {
            for (turn in history.recent(HISTORY_TURNS)) {
                messages += ChatMessage(if (turn.role == "user") Role.USER else Role.ASSISTANT, turn.text)
            }
        }
        messages += ChatMessage(Role.USER, text)

        var lastResult: ToolResult? = null
        repeat(MAX_AI_STEPS) { stepIndex ->
            when (val r = client.chat(messages, tools)) {
                is AiResult.Error -> return Reply(aiError(r), success = false, suggestScreen = if (r.kind == AiErrorKind.AUTH || r.kind == AiErrorKind.NOT_CONFIGURED) "settings" else null)
                is AiResult.Ok -> {
                    if (r.toolCalls.isEmpty()) {
                        val answer = r.text.ifBlank { lastResult?.message ?: "I'm not sure how to help with that." }
                        return Reply(clean(answer), expectReply = answer.trimEnd().endsWith("?"), directive = lastResult?.directive, success = lastResult?.success ?: true, suggestScreen = lastResult?.suggestScreen)
                    }
                    messages += ChatMessage(Role.ASSISTANT, r.text, r.toolCalls)
                    for ((i, call) in r.toolCalls.withIndex()) {
                        when (val step = runOne(call, emptyList(), env, confirmed = false)) {
                            is Step.Halt -> return step.reply
                            is Step.Done -> {
                                val res = step.result
                                lastResult = res
                                if (step.private) markPrivate()
                                val spec = registry.get(call.name)?.spec
                                // A single action speaks for itself: use the tool's own verified wording
                                // instead of paying for (and trusting) a second model round trip.
                                val single = r.toolCalls.size == 1 && stepIndex == 0
                                if (res.expectReply || !res.success && single || single && spec != null && !spec.query) {
                                    var replyText = res.message
                                    var expect = res.expectReply
                                    val offer = res.offer ?: if (res.success && call.name == ToolIds.PLAY_MUSIC) musicOffer(call) else null
                                    if (offer != null && res.success && !expect) {
                                        pending = Pending.Offer(offer)
                                        replyText = "$replyText ${offer.question}"
                                        expect = true
                                    }
                                    return Reply(replyText, expect, res.directive, res.success, res.suggestScreen)
                                }
                                val content = when {
                                    step.private -> "OK: the content was read to the user directly and is withheld from you for privacy."
                                    res.success -> "OK: ${res.message}"
                                    else -> "FAILED: ${res.message}"
                                }
                                messages += ChatMessage(Role.TOOL, content, toolCallId = call.id, toolName = call.name)
                                if (i == r.toolCalls.lastIndex && step.private && r.toolCalls.size == 1) return Reply(res.message, res.expectReply, res.directive, res.success, res.suggestScreen)
                            }
                        }
                    }
                }
            }
        }
        val res = lastResult
        return Reply(res?.message ?: "I couldn't finish that.", success = res?.success ?: false, suggestScreen = res?.suggestScreen)
    }

    private fun aiError(e: AiResult.Error): String = when (e.kind) {
        AiErrorKind.NETWORK -> "I couldn't reach the AI service, so I can't answer that right now. Reminders, calls and other phone actions still work offline."
        AiErrorKind.AUTH -> "The AI provider rejected the API key. Please check it in Lumi's settings."
        AiErrorKind.RATE_LIMIT -> "The AI provider is rate limiting me right now. Try again in a moment."
        AiErrorKind.NOT_CONFIGURED -> e.message + " You can set one up in Lumi's settings."
        AiErrorKind.BAD_RESPONSE -> "The AI service had a problem: ${e.message}"
    }

    /** Strips markdown that would be read aloud literally. */
    private fun clean(text: String): String =
        text.replace(Regex("[*_`#]+"), "").replace(Regex("\\n{2,}"), "\n").trim()

    private suspend fun systemPrompt(env: TurnEnv): String {
        val now = time.now()
        val sb = StringBuilder()
        sb.append(
            "You are Lumi, a personal voice assistant running on the user's Android phone. " +
                "Personality: friendly, calm, intelligent, lightly witty, never verbose. " +
                "Your replies are spoken aloud: use one to three short sentences, plain text, no markdown, lists or emoji.\n" +
                "Rules:\n" +
                "- Act through the provided tools. Never claim something happened unless a tool result says so.\n" +
                "- If a tool result starts with FAILED, tell the user plainly what went wrong and what they can do.\n" +
                "- Ask one brief clarifying question when a request is ambiguous.\n" +
                "- Never invent personal facts about the user.\n" +
                "- For dates and times, pass the user's own words (for example \"tomorrow 5pm\") to tools; do not calculate dates yourself.\n" +
                "- To send any message, create a draft with a draft tool. The user confirms sending by voice; you cannot send.\n" +
                "- Reminders are always tasks: use create_task for anything the user wants to be reminded of.\n",
        )
        sb.append("Current local date and time: ${now.dayOfWeek.getDisplayName(TextStyle.FULL, Locale.ENGLISH)}, ${now.toLocalDate()} ${TimeFormat.time(now.toLocalTime().withSecond(0).withNano(0))}.\n")
        if (env.deviceLocked) {
            sb.append("The phone is locked. Only the listed tools are available, and you must not reveal personal information.\n")
        } else {
            val facts = memory.contextSnippets(25)
            if (facts.isNotEmpty()) {
                sb.append("What the user has told you about themselves (use naturally, do not recite):\n")
                for (f in facts) sb.append("- ").append(f.take(200)).append('\n')
            }
        }
        return sb.toString()
    }

    companion object {
        const val MAX_AI_STEPS = 5
        const val HISTORY_TURNS = 8
    }
}

/** [TextReviser] backed by whichever AI provider is configured. */
class AiTextReviser(private val ai: () -> AiClient?) : TextReviser {
    override suspend fun revise(text: String, instruction: String): String? {
        val client = ai() ?: return null
        val messages = listOf(
            ChatMessage(Role.SYSTEM, "You rewrite short messages. Apply the instruction to the message and reply with only the rewritten message text, with no quotes and no commentary."),
            ChatMessage(Role.USER, "Instruction: $instruction\nMessage: $text"),
        )
        return when (val r = client.chat(messages, emptyList())) {
            is AiResult.Ok -> r.text.trim().takeIf { it.isNotEmpty() }
            is AiResult.Error -> null
        }
    }
}
