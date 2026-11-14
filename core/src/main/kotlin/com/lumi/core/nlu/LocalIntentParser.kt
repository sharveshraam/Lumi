package com.lumi.core.nlu

import com.lumi.core.ai.ToolCall
import com.lumi.core.model.TimeSource
import com.lumi.core.model.now
import com.lumi.core.time.DateTimeParser
import com.lumi.core.time.TimeDefaults
import com.lumi.core.tools.Args
import com.lumi.core.tools.Directive
import com.lumi.core.tools.ToolIds
import java.util.concurrent.ConcurrentHashMap

enum class SmallTalk { GREETING, THANKS, IDENTITY, HELP, TIME, DATE, HOW_ARE_YOU }

/** What the on-device parser understood. Null from [LocalIntentParser.parse] means "ask the AI". */
sealed class LocalIntent {
    data class Calls(val calls: List<ToolCall>) : LocalIntent()
    data class Control(val directive: Directive, val say: String = "") : LocalIntent()
    data class Chat(val kind: SmallTalk) : LocalIntent()
    object StartOnboarding : LocalIntent()
}

/**
 * Deterministic, offline understanding of common phone commands. It maps natural phrasing onto
 * the same structured tool calls an AI planner would produce, so everyday requests work with no
 * network, no API key and no latency. Anything it does not recognise is left to the AI.
 */
class LocalIntentParser(
    private val time: TimeSource,
    private val defaults: () -> TimeDefaults = { TimeDefaults() },
) {
    private val cache = ConcurrentHashMap<String, Regex>()
    private fun rx(p: String): Regex = cache.getOrPut(p) { Regex(p, RegexOption.IGNORE_CASE) }
    private fun m(p: String, s: String): MatchResult? = rx(p).matchEntire(s)
    private fun has(p: String, s: String): Boolean = rx(p).containsMatchIn(s)

    private fun call(tool: String, vararg args: Pair<String, Any?>) = ToolCall("local", tool, Args.of(*args))
    private fun one(tool: String, vararg args: Pair<String, Any?>): LocalIntent = LocalIntent.Calls(listOf(call(tool, *args)))

    fun isYes(utterance: String): Boolean = m(YES, normalise(utterance)) != null
    fun isNo(utterance: String): Boolean = m(NO, normalise(utterance)) != null

    /** Strips wake words, politeness and trailing punctuation. */
    fun normalise(raw: String): String {
        var s = raw.trim().replace('\u2019', '\'')
        s = rx("^(?:(?:hey|ok|okay|hi|hello|yo)[, ]+)?lumi[,!.]*\\s+(?=\\S)").replace(s, "")
        s = rx("^(?:please|kindly)[, ]+").replace(s, "")
        s = rx("^(?:can|could|would|will) you (?:please |kindly )?").replace(s, "")
        s = rx("^(?:i want you to|i'd like you to|i would like you to|i need you to|go ahead and)\\s+").replace(s, "")
        s = rx("^(?:please|kindly)[, ]+").replace(s, "")
        s = s.trim().trimEnd('.', '!', '?', ',').trim()
        if (m("(?:please|thanks|thank you)", s) == null) s = rx("[,\\s]+(?:please|for me)$").replace(s, "")
        return s.trim()
    }

    /** Commands that only make sense while a dictated draft is waiting for confirmation. */
    fun parseDraftCommand(utterance: String): LocalIntent? {
        val s = normalise(utterance)
        if (m("(?:yes[, ]*|ok(?:ay)?[, ]*)?(?:send|send it|send that|send the (?:message|reply|email|text)|go ahead and send(?: it)?|deliver it|submit(?: it)?|post it|type it|enter it|looks good|that's good|that's fine|that's perfect|perfect)(?: now)?", s) != null || isYes(s)) {
            return one(ToolIds.SEND_DRAFT)
        }
        if (m("(?:read|say)(?: it| that| the (?:message|reply|draft|email|text))?(?: back)?(?: again)?(?: to me)?|what does it say|read the reply again", s) != null) {
            return one(ToolIds.READ_DRAFT)
        }
        if (m("(?:discard|delete|cancel|scrap|drop|forget)(?: it| that| the (?:message|reply|draft|email|text))?|don't send(?: it)?|do not send(?: it)?", s) != null || isNo(s)) {
            return one(ToolIds.DISCARD_DRAFT)
        }
        m("(?:change (?:it|that|the message|the reply) to|make it say|replace (?:it|that) with|instead say|say|no,? say|actually,? say|rewrite it as|scratch that,?)\\s+(.+)", s)?.let {
            return one(ToolIds.EDIT_DRAFT, "text" to it.groupValues[1], "mode" to "replace")
        }
        m("(?:add|append|also say|and add|also add)\\s+(.+)", s)?.let {
            return one(ToolIds.EDIT_DRAFT, "text" to it.groupValues[1], "mode" to "append")
        }
        if (m("(?:make|rewrite|rephrase|turn) (?:it|that|this|the message|the reply|the email)\\b.+", s) != null ||
            m("(?:shorten|shorter|more polite|politer|more formal|more casual|friendlier|fix (?:the )?(?:grammar|spelling|punctuation)|correct (?:it|the grammar))(?: it)?", s) != null
        ) {
            return one(ToolIds.REVISE_DRAFT, "instruction" to s)
        }
        return null
    }

    fun parse(utterance: String): LocalIntent? {
        val s = normalise(utterance)
        if (s.isEmpty()) return null
        return control(s)
            ?: chat(s)
            ?: assistantInfo(s)
            ?: notifications(s)
            ?: messages(s)
            ?: reminders(s)
            ?: recall(s)
            ?: listTasks(s)
            ?: taskChanges(s)
            ?: memory(s)
            ?: calls(s)
            ?: media(s)
            ?: utilities(s)
            ?: screen(s)
            ?: apps(s)
            ?: looseTask(s)
    }

    // ------------------------------------------------------------------ session control

    private fun control(s: String): LocalIntent? {
        if (m("stop|stop it|stop talking|stop speaking|be quiet|quiet|shut up|shush|enough|silence|hush", s) != null) {
            return LocalIntent.Control(Directive.StopSpeaking)
        }
        if (m("cancel|never ?mind|forget it|dismiss|close|go away|that's all|that is all|that'll be all|that will be all|bye|goodbye|bye bye|exit|i'm done|nothing|nothing else|no thanks|no thank you|not now", s) != null) {
            return LocalIntent.Control(Directive.Dismiss, "Okay.")
        }
        if (m("repeat|repeat that|say that again|say it again|come again|pardon|sorry\\??|what did you say|what was that|repeat please|once more|one more time", s) != null) {
            return LocalIntent.Control(Directive.Repeat)
        }
        m("(?:speak|talk|go)(?: a (?:bit|little))? (faster|quicker|slower|more slowly|more quickly)|(slow down|speed up)", s)?.let {
            val word = (it.groupValues[1] + it.groupValues[2]).lowercase()
            val faster = word.startsWith("fast") || word.startsWith("quick") || word == "more quickly" || word == "speed up"
            return LocalIntent.Control(Directive.SpeechRate(if (faster) 1 else -1), if (faster) "Okay, speaking faster." else "Okay, I'll slow down.")
        }
        if (m("(?:speak|talk)(?: at)?(?: a)? normal(?:ly| speed| pace)?|normal speed|reset (?:the |your )?(?:speed|speech rate)", s) != null) {
            return LocalIntent.Control(Directive.SpeechRate(0), "Back to normal speed.")
        }
        if (m("open (?:the )?lumi(?: app)?|show (?:me )?(?:the )?full app|open the (?:full )?app", s) != null) {
            return LocalIntent.Control(Directive.OpenScreen("home"), "Opening Lumi.")
        }
        m("(?:open|show)(?: me)? (?:the |my )?(?:lumi )?(task|reminder|memory|memories|permission|setting|place|diagnostic)s?(?: screen| page| list| manager)?(?: in lumi)?", s)?.let {
            val word = it.groupValues[1].lowercase()
            val lumiNamed = has("lumi|screen|page|manager", s)
            // "open settings" means Android settings; only "open Lumi settings" opens ours.
            if (word == "setting" && !has("lumi", s)) return null
            if ((word == "task" || word == "reminder") && !lumiNamed && s.lowercase().startsWith("show")) return null
            val screen = when (word) {
                "task", "reminder" -> "tasks"
                "memory", "memories" -> "memory"
                "permission" -> "permissions"
                "place" -> "places"
                "diagnostic" -> "diagnostics"
                else -> "settings"
            }
            return LocalIntent.Control(Directive.OpenScreen(screen), "Opening $screen.")
        }
        return null
    }

    private fun chat(s: String): LocalIntent? {
        if (m("(?:hi|hello|hey|hey there|hi there|yo|hello there|good (?:afternoon|evening)|lumi)(?: lumi)?", s) != null) return LocalIntent.Chat(SmallTalk.GREETING)
        if (m("(?:thanks|thank you|thanks a lot|thank you so much|thank you very much|cheers|great thanks|ok thanks|okay thanks)(?: lumi)?", s) != null) return LocalIntent.Chat(SmallTalk.THANKS)
        if (m("who are you|what are you|what's your name|what is your name|introduce yourself", s) != null) return LocalIntent.Chat(SmallTalk.IDENTITY)
        if (m("help|help me|what can you do|what can i say|what can i ask(?: you)?|what are you capable of|how can you help(?: me)?|what do you do", s) != null) return LocalIntent.Chat(SmallTalk.HELP)
        if (m("how are you(?: doing)?(?: today)?|how's it going|what's up", s) != null) return LocalIntent.Chat(SmallTalk.HOW_ARE_YOU)
        if (m("what(?:'s| is)? ?(?:the )?(?:current )?time(?: is it)?(?: now| right now)?|what time is it(?: now| right now)?|(?:tell me )?the time|time please", s) != null) return LocalIntent.Chat(SmallTalk.TIME)
        if (m("what(?:'s| is) (?:the |today's |todays )?(?:date|day)(?: today| is it(?: today)?)?|what day is it(?: today)?|what(?:'s| is) today", s) != null) return LocalIntent.Chat(SmallTalk.DATE)
        return null
    }

    private fun assistantInfo(s: String): LocalIntent? {
        if (m("good morning(?: lumi)?|(?:give me |start |read |tell me |what's |what is )?(?:my |the |today's )?(?:daily |morning )?(?:briefing|brief|rundown|summary of (?:my|the) day|update for today)|brief me|what(?:'s| does) my day look(?:ing)? like|how(?:'s| is) my day looking", s) != null) {
            return one(ToolIds.BRIEFING)
        }
        if (m("(?:what(?:'s| is) (?:my |the )?(?:phone(?:'s)? )?battery(?: level| percentage| status| life)?(?: at| like)?|how much (?:battery|charge)(?: do i have)?(?: is)?(?: left)?|battery(?: level| status| percentage)?|is my phone charging)", s) != null) {
            return one(ToolIds.BATTERY)
        }
        m("what(?:'s| is) on my calendar(?: for)?(?: (today|tomorrow))?|(?:do i have |have i got |are there )?any (?:meetings|events|appointments)(?: (today|tomorrow))?|what (?:meetings|events|appointments) do i have(?: (today|tomorrow))?|(?:read|show)(?: me)? my calendar(?: for)?(?: (today|tomorrow))?", s)?.let {
            val day = it.groupValues.drop(1).firstOrNull { g -> g.isNotBlank() }?.lowercase() ?: "today"
            return one(ToolIds.CALENDAR, "day" to day)
        }
        if (m("(?:start|begin|continue|resume|run|do|restart) (?:the |my )?(?:setup|onboarding|introduction)|set ?up lumi|set lumi up|let's get to know each other|get to know me|learn about me|set me up|ask me (?:some |the )?(?:setup )?questions", s) != null) {
            return LocalIntent.StartOnboarding
        }
        if (m("(?:stop|disable|turn off|pause) (?:the )?learning(?: about me| my habits)?|stop learning(?: about me| my habits)?|don't learn(?: about me| anything)?", s) != null) {
            return one(ToolIds.SET_LEARNING, "enabled" to false)
        }
        if (m("(?:start|enable|turn on|resume) (?:the )?learning(?: about me)?", s) != null) return one(ToolIds.SET_LEARNING, "enabled" to true)
        return null
    }

    // ------------------------------------------------------------------ notifications & messages

    private fun notifications(s: String): LocalIntent? {
        if (!has("notification", s) && m("what did i miss|did i miss anything|anything new", s) == null) return null
        m("(?:ignore|mute|silence|stop reading|block|don't read|do not read|hide) (?:the )?notifications? (?:from|for|of) (.+)", s)?.let {
            val app = it.groupValues[1].trim()
            val current = m("(?:this|that|the current) (?:app|application|one)|it|this", app) != null
            return one(ToolIds.IGNORE_APP, "app" to if (current) "current" else app)
        }
        if (m("(?:pause|stop|disable|turn off|suspend) (?:all )?(?:notification (?:processing|reading|access|intelligence|listening)|(?:reading |processing )?(?:my )?notifications)", s) != null) {
            return one(ToolIds.PAUSE_NOTIFICATIONS, "paused" to true)
        }
        if (m("(?:resume|start|enable|turn on|unpause) (?:all )?(?:notification (?:processing|reading|access|intelligence|listening)|(?:reading |processing )?(?:my )?notifications)", s) != null) {
            return one(ToolIds.PAUSE_NOTIFICATIONS, "paused" to false)
        }
        m("(?:summari[sz]e|sum up|recap)(?: (?:my|the|today's|todays|all|all my))* notifications?(?: (?:from|for) (.+))?|(?:give me )?(?:a )?(?:summary|rundown|recap|overview) of (?:my |the |today's |todays )?notifications?(?: (?:from|for) (.+))?|notifications? summary", s)?.let {
            val app = it.groupValues.drop(1).firstOrNull { g -> g.isNotBlank() }
            return one(ToolIds.NOTIFICATIONS, "mode" to "summary", "app" to cleanApp(app))
        }
        if (m("(?:do i have |have i got |are there |read |show |check |tell me )?(?:me )?(?:my |the |any )*(?:important|urgent|priority)(?: new)? notifications?", s) != null) {
            return one(ToolIds.NOTIFICATIONS, "mode" to "important")
        }
        m("(?:read|check|show|tell me|what are|list|go through)(?: me)?(?: out)?(?: (?:my|the|all|any|all my))*(?: (?:new|latest|recent|unread|today's|todays))? notifications?(?: (?:from|for|on) (.+))?", s)?.let {
            return one(ToolIds.NOTIFICATIONS, "mode" to "read", "app" to cleanApp(it.groupValues[1]))
        }
        if (m("(?:do i have |have i got |are there )?(?:any )?(?:new |unread )?notifications?|what notifications do i have|what did i miss|did i miss anything|anything new", s) != null) {
            return one(ToolIds.NOTIFICATIONS, "mode" to "read")
        }
        return null
    }

    private fun cleanApp(raw: String?): String? {
        val a = raw?.trim().orEmpty()
        if (a.isEmpty()) return null
        if (m("today|this morning|tonight|now", a) != null) return null
        return a
    }

    private val messageApps = "whatsapp|sms|text|telegram|signal|messenger|instagram|slack|discord|teams|gmail|email"

    private fun messages(s: String): LocalIntent? {
        // Email composition.
        m("(?:send|write|compose|draft|start)\\s+(?:an? |a new )?(?:e-?mail|mail)\\s+(?:to\\s+)?(.+?)(?:\\s+(?:about|regarding|with (?:the )?subject)\\s+(.+?))?(?:\\s+(?:saying|that says|and say|with (?:the )?(?:body|message))\\s+(.+))?", s)?.let {
            return one(ToolIds.DRAFT_EMAIL, "to" to it.groupValues[1].trim(), "subject" to it.groupValues[2].trim().ifEmpty { null }, "body" to it.groupValues[3].trim().ifEmpty { null })
        }
        // Reading.
        m("read (?:it|that|the message|that message|the last message)(?: (?:again|once more|one more time))", s)?.let { return one(ToolIds.READ_MESSAGES, "again" to true) }
        m("(?:read|check|show|open|what(?:'s| is| are)|tell me)(?: me)?(?: out)?(?: (?:my|the|any))?(?: (latest|last|newest|recent|new|unread|most recent))?(?: ($messageApps))?\\s+(?:messages?|texts?|chats?|dms?)(?: (?:from|by) (.+))?", s)?.let {
            val app = it.groupValues[2].lowercase().takeIf { a -> a.isNotEmpty() && a != "text" }
            return one(ToolIds.READ_MESSAGES, "from" to it.groupValues[3].trim().ifEmpty { null }, "app" to app)
        }
        m("(?:do i have |are there |have i got )?(?:any )?(?:new |unread )(?:messages?|texts?)(?: (?:from|by) (.+))?", s)?.let {
            return one(ToolIds.READ_MESSAGES, "from" to it.groupValues[1].trim().ifEmpty { null })
        }
        m("(?:did|has) (.+?) (?:message|text|reply|replied|messaged|texted|written|write)(?: to)?(?: me)?(?: back)?(?: yet)?", s)?.let {
            return one(ToolIds.READ_MESSAGES, "from" to it.groupValues[1].trim())
        }
        m("what did (.+?) (?:say|send|write|text)(?: me)?", s)?.let { return one(ToolIds.READ_MESSAGES, "from" to it.groupValues[1].trim()) }
        // Replying.
        m("(?:reply|respond|answer|write back|text back|reply back|message back)(?: to (?:him|her|them|it|that|this))?(?: (?:saying|with|that|and say|by saying|and tell (?:him|her|them)))?[,:]?\\s+(.+)", s)?.let {
            return one(ToolIds.DRAFT_REPLY, "text" to it.groupValues[1].trim())
        }
        if (m("(?:reply|respond|answer|write back|text back)(?: to (?:him|her|them|it|that|this))?", s) != null) return one(ToolIds.DRAFT_REPLY)
        // Composing a new message.
        m("(?:send|write|text|message|compose|draft|drop|shoot)\\s+(?:an? )?(?:($messageApps)\\s+)?(?:message |text |msg |dm )?(?:to\\s+)?(.+?)\\s+(?:on ($messageApps)\\s+)?(?:saying|that says|which says|telling (?:him|her|them)(?: that)?|to say|and say|:)\\s+(.+)", s)?.let {
            val app = (it.groupValues[1] + it.groupValues[3]).lowercase().ifEmpty { null }
            return one(ToolIds.DRAFT_MESSAGE, "to" to it.groupValues[2].trim(), "text" to it.groupValues[4].trim(), "app" to app)
        }
        m("(whatsapp|text|message|tell|dm|ping) (\\S+(?: \\S+)?) (?:that|saying|to say) (.+)", s)?.let {
            val to = it.groupValues[2].trim()
            if (m("me|us|myself|you|yourself", to) == null) {
                val verb = it.groupValues[1].lowercase()
                return one(ToolIds.DRAFT_MESSAGE, "to" to to, "text" to it.groupValues[3].trim(), "app" to if (verb == "whatsapp") "whatsapp" else if (verb == "text") "sms" else null)
            }
        }
        m("(?:send|write|compose|draft)\\s+(?:an? )?(?:($messageApps)\\s+)?(?:message|text|msg|dm)\\s+(?:to|for)\\s+(.+?)(?:\\s+on ($messageApps))?", s)?.let {
            val app = (it.groupValues[1] + it.groupValues[3]).lowercase().ifEmpty { null }
            return one(ToolIds.DRAFT_MESSAGE, "to" to it.groupValues[2].trim(), "app" to app)
        }
        m("(?:type|dictate|enter text|input)\\s+(.+?)(?:\\s+(?:here|in (?:this|the) (?:field|box|text box)))?", s)?.let {
            return one(ToolIds.TYPE_TEXT, "text" to it.groupValues[1].trim())
        }
        return null
    }

    // ------------------------------------------------------------------ reminders & tasks

    private val locationPhrase =
        "\\b(?:when|once|as soon as|after|the moment|whenever|next time)\\s+i(?:'m| am)?\\s+(reach(?:ing)?|get(?:ting)? to|arrive(?:d)? (?:at|in)|get(?:ting)? back(?: to)?|get|come|at|in|enter|leave|leaving|exit|get out of)\\s+(?:back\\s+)?(?:to\\s+)?(?:my\\s+|the\\s+)?([\\p{L}'][\\p{L}' ]*?)(?=\\s+(?:to|about|that)\\s+|\\s*$|\\s*,)"

    private fun reminders(s: String): LocalIntent? {
        m("keep reminding me(?: about (.+?))?(?: every (.+?))?(?: (?:until|till|unless|if) (.*))?", s)?.let {
            val minutes = it.groupValues[2].takeIf { g -> g.isNotBlank() }?.let { g -> DateTimeParser.parseDuration(g)?.toMinutes() }
            val fromClause = m("i(?:'ve| have| am|'m)? (?:finish(?:ed)?|complet(?:e|ed)|do|done)(?: with)? (.+)", it.groupValues[3].trim())?.groupValues?.get(1).orEmpty()
            val query = pronounToEmpty(it.groupValues[1]).ifEmpty { pronounToEmpty(taskRef(fromClause)) }
            return one(ToolIds.SET_NAG, "query" to query, "minutes" to minutes)
        }
        val head = m("(?:remind me|set (?:a|another|up a|me a) reminder|create (?:a |another )?reminder|add (?:a |another )?reminder|make (?:a )?reminder|i need a reminder|give me a reminder|put (?:a )?reminder|reminder|don't let me forget|do not let me forget)\\b[,:]?\\s*(.*)", s)
        if (head != null) return reminderBody(head.groupValues[1], task = false, important = false)

        m("(?:remember|don't forget|do not forget|make sure i remember)\\s+(to\\s+.+)", s)?.let { return reminderBody(it.groupValues[1], task = false, important = false) }

        m("(?:add|create|make|new|start)\\s+(?:a\\s+|an\\s+|another\\s+)?(?:new\\s+)?(?:(important|urgent|high[- ]priority)\\s+)?(?:task|to-?do|to do)(?:\\s+item)?\\b(?:\\s+(?:to|for|called|named|saying))?[,:]?\\s*(.*)", s)?.let {
            return reminderBody(it.groupValues[2], task = true, important = it.groupValues[1].isNotBlank())
        }
        m("(?:add|put)\\s+(.+?)\\s+(?:to|on|in)\\s+(?:my\\s+|the\\s+)?(?:to-?do|to do|task)s?(?:\\s+list)?(.*)", s)?.let {
            return reminderBody(it.groupValues[1] + " " + it.groupValues[2], task = true, important = false)
        }
        m("(?:todo|to-do|task)[:,]?\\s+(.+)", s)?.let { return reminderBody(it.groupValues[1], task = true, important = false) }
        return null
    }

    private fun pronounToEmpty(text: String): String {
        val t = text.trim()
        return if (m("(?:it|this|that|this one|that one|(?:my|the|this|that) (?:reminder|task|one))?", t) != null) "" else t
    }

    private fun reminderBody(raw: String, task: Boolean, important: Boolean): LocalIntent {
        var body = raw.trim()
        var high = important
        val now = time.now()

        // "remind me again if I haven't completed this"
        if (has("^(?:me\\s+)?(?:about (?:it|this|that)\\s+)?again\\b.*\\b(?:if|until|till|unless)\\b", body) || has("\\b(?:if|until|unless) i (?:haven't|have not|don't|do not|didn't)\\b", body) && has("^(?:again|about (?:it|this|that))\\b", body)) {
            val minutes = rx("\\bevery\\s+(.+?)(?:\\s+(?:if|until|till|unless)\\b|$)").find(body)?.let { DateTimeParser.parseDuration(it.groupValues[1])?.toMinutes() }
            return one(ToolIds.SET_NAG, "query" to "", "minutes" to minutes)
        }
        // "remind me again in 10 minutes", "remind me about this tomorrow", "remind me later"
        if (has("^(?:me\\s+)?(?:again\\s+)?about (?:it|this|that)\\b", body)) {
            rx(locationPhrase).find(body)?.let { loc ->
                val verb = loc.groupValues[1].lowercase()
                val leaving = verb.startsWith("leav") || verb.startsWith("exit") || verb.startsWith("get out")
                return one(ToolIds.ADD_REMINDER, "query" to "", "place" to loc.groupValues[2].trim(), "on_leave" to leaving)
            }
        }
        m("(?:about (?:it|this|that)\\s*)?(?:again\\s*)?(?:about (?:it|this|that)\\s*)?(.*)", body)?.let {
            val refersBack = has("^(?:again\\b|about (?:it|this|that)\\b)", body) || m("later|in a (?:bit|while)|after some time", body) != null
            if (refersBack) {
                val rest = it.groupValues[1].trim()
                return if (rest.isEmpty() || m("later|in a (?:bit|while)|after some time|again", rest) != null) {
                    one(ToolIds.POSTPONE_TASK, "query" to "", "minutes" to 60)
                } else {
                    one(ToolIds.POSTPONE_TASK, "query" to "", "by" to rest)
                }
            }
        }

        rx("(?:,|\\s-|\\s)\\s*(?:it'?s|it is|this is|that'?s|which is)\\s+(?:really |very |super |quite )?(?:important|urgent)$|\\s+(?:with |as )?high priority$").find(body)?.let {
            high = true
            body = body.removeRange(it.range).trim()
        }
        rx("^(?:an? )?(?:important|urgent)(?: one)?[,:]?\\s+(?=to |about |that )").find(body)?.let {
            high = true
            body = body.removeRange(it.range).trim()
        }

        var place: String? = null
        var leave = false
        rx(locationPhrase).find(body)?.let { loc ->
            val verb = loc.groupValues[1].lowercase()
            place = loc.groupValues[2].trim()
            leave = verb.startsWith("leav") || verb.startsWith("exit") || verb.startsWith("get out")
            body = (body.substring(0, loc.range.first) + " " + body.substring(loc.range.last + 1)).trim().trim(',').trim()
        }

        body = rx("^(?:me\\s+)?(?:to|about|that|for)\\s+").replace(body, "")
        val parsed = DateTimeParser.parse(body, now, defaults())
        var title = rx("^(?:to|about|that|for)\\s+").replace(parsed.remainder, "").trim()
        title = rx("\\s+(?:to|about|at|on|by|for)$").replace(title, "").trim()

        val placeName = place
        if (placeName != null) {
            val here = m("(?:this|that|my current|the current|current) (?:location|place|spot|position|area)|here|this", placeName) != null
            val calls = mutableListOf<ToolCall>()
            val name = if (here) "this spot" else placeName
            if (here) calls += call(ToolIds.SAVE_PLACE, "name" to name)
            calls += call(
                ToolIds.CREATE_TASK,
                "title" to title.ifEmpty { null },
                "place" to name,
                "on_leave" to leave,
                "priority" to if (high) "high" else null,
            )
            return LocalIntent.Calls(calls)
        }

        return one(
            ToolIds.CREATE_TASK,
            "title" to title.ifEmpty { null },
            "when" to parsed.dateTime?.toString(),
            "recurrence" to parsed.recurrence?.encode(),
            "priority" to if (high) "high" else null,
            "reminder" to if (parsed.dateTime != null) null else !task,
        )
    }

    private fun recall(s: String): LocalIntent? {
        m("what (?:do|did) you (?:know|remember|have)(?: saved)? about (.+)", s)?.let {
            val topic = it.groupValues[1].trim()
            return if (m("me|myself", topic) != null) one(ToolIds.RECALL, "category" to "all") else one(ToolIds.RECALL, "query" to topic)
        }
        if (has("\\b(?:ask(?:ed)?|told|tell) you to (?:remember|keep in mind|note)\\b", s) || m("what (?:do|did) you remember|what have you (?:remembered|memori[sz]ed|learned|learnt)(?: about me)?|what(?:'s| is) in your memory", s) != null) {
            val about = rx("\\babout (.+)$").find(s)?.groupValues?.get(1)?.trim()
            return if (about != null && m("me|myself", about) == null) one(ToolIds.RECALL, "query" to about) else one(ToolIds.RECALL)
        }
        m("(?:what(?:'s| is| was| are)|where(?:'s| is| are| did i (?:put|keep|leave|park))|tell me|do you (?:know|remember)) (?:my|our) (.+)", s)?.let {
            val topic = it.groupValues[1].trim()
            if (m("(?:next |first |last |\\w+ )?(?:tasks?|reminders?|schedule|agenda|plans?|to-?dos?|to-?do list|day|week|battery|location|notifications?|messages?|calendar|alarms?|timers?|meetings?|events?|appointments?|screen|volume|briefing)\\b.*", topic) == null) {
                return one(ToolIds.RECALL, "query" to topic)
            }
        }
        if (m("(?:show|list|read|tell)(?: me)? (?:my |the |all my )?(?:memories|saved memories)", s) != null) return one(ToolIds.RECALL, "category" to "all")
        if (m("(?:show|list|read|tell)(?: me)? (?:my |the |all my )?notes", s) != null) return one(ToolIds.RECALL, "category" to "notes")
        return null
    }

    private fun listTasks(s: String): LocalIntent? {
        if (has("\\b(?:location|place)(?:-| )?(?:based )?reminders\\b|\\breminders?\\b.*\\b(?:near|around|at|for) (?:my |the )?(?:home|college|work|office|school|gym|hostel|university|campus)\\b", s) &&
            m("(?:what|which|show|list|tell|read|do i have|any|my)\\b.*", s) != null
        ) {
            return one(ToolIds.LIST_LOCATION_REMINDERS)
        }
        val isList =
            m("what (?:do i have|have i got|am i (?:doing|supposed to do)|is there|should i do|do i need to do|do i have to do|are my (?:\\w+ )?(?:tasks|reminders|plans|to-?dos)|(?:is|'s) on my (?:plate|list|agenda|schedule|to-?do list)|(?:is|'s) due|(?:is|'s) happening|(?:is|'s) planned|(?:is|'s) (?:pending|overdue|left|next|coming up))\\b.*", s) != null ||
                m("what(?:'s| is)? ?(?:on|in) my (?:plate|list|agenda|schedule|to-?do list)\\b.*", s) != null ||
                m("(?:what|which) (?:tasks|reminders|things|to-?dos|chores)\\b.*", s) != null ||
                m("(?:show|list|tell|read|give|check)(?: me)?(?: all)?(?: of)? (?:my |the |all my )?(?:all )?(?:(?:pending|overdue|completed|done|finished|upcoming|important|urgent|recurring|repeating|open|remaining|today's|tomorrow's|todays|tomorrows|this week's)\\s+)?(?:tasks?|reminders?|to-?dos?|to-?do list|task list|agenda|schedule)\\b.*", s) != null ||
                m("(?:do i have |have i got |are there |is there )?any(?:thing)?(?: (?:tasks?|reminders?|due|planned|scheduled|to do|pending|overdue|left|on|for|i need to do))*\\b(?: (?:today|tonight|tomorrow|this week|on .+|for .+))?", s) != null ||
                m("(?:my )?(?:tasks|reminders|to-?do list|agenda|pending tasks|overdue tasks)(?: for)?(?: .+)?", s) != null ||
                m("am i forgetting (?:anything|something)|what am i forgetting|what(?:'s| is) (?:overdue|pending|left|due|planned|coming up)\\b.*|what(?:'s| is) next(?: on my list)?", s) != null
        if (!isList) return null
        val l = s.lowercase()
        val filter = when {
            has("\\boverdue\\b|\\bmissed\\b|\\blate\\b|forgetting", l) -> "overdue"
            has("\\bcompleted\\b|\\bfinished\\b|\\bdone\\b", l) -> "completed"
            has("\\bimportant\\b|\\burgent\\b|\\bpriority\\b", l) -> "important"
            has("\\brecurring\\b|\\brepeating\\b", l) -> "recurring"
            has("\\bupcoming\\b|coming up|this week|next few days|\\bsoon\\b", l) -> "week"
            has("\\btoday\\b|\\btonight\\b|todays|today's|\\bnow\\b", l) -> "today"
            has("\\btomorrow\\b|tomorrows|tomorrow's", l) && !has("day after tomorrow", l) -> "tomorrow"
            else -> null
        }
        if (filter != null) return one(ToolIds.LIST_TASKS, "filter" to filter)
        val parsed = DateTimeParser.parse(s, time.now(), defaults())
        if (parsed.hasDate && parsed.dateTime != null && parsed.recurrence == null) {
            return one(ToolIds.LIST_TASKS, "date" to parsed.dateTime.toLocalDate().toString())
        }
        if (has("\\bpending\\b|\\bopen\\b|\\bremaining\\b|\\ball\\b|\\beverything\\b|\\bleft\\b", l)) return one(ToolIds.LIST_TASKS, "filter" to "pending")
        val showVerb = m("(?:show|list|tell|read|give|check)\\b.*|(?:my )?(?:tasks|reminders|to-?do list|agenda).*", s) != null
        return one(ToolIds.LIST_TASKS, "filter" to if (showVerb) "pending" else "today")
    }

    private fun taskRef(raw: String): String {
        var q = raw.trim()
        q = rx("^(?:my|the|that|this)\\s+").replace(q, "")
        q = rx("^(?:reminder|task|to-?do)\\s+(?:about|for|to|called|named)\\s+").replace(q, "")
        q = rx("\\s+(?:reminder|task|to-?do|item)$").replace(q, "")
        return pronounToEmpty(q)
    }

    private fun taskChanges(s: String): LocalIntent? {
        // Priority and edits first: "mark X as important" must not be read as completion.
        m("(?:mark|make|flag|set|tag) (.+?) (?:as |to )?(?:an? )?(important|urgent|high priority|top priority|low priority|normal priority|not important)", s)?.let {
            val p = when (it.groupValues[2].lowercase()) {
                "low priority", "not important" -> "low"
                "normal priority" -> "normal"
                else -> "high"
            }
            return one(ToolIds.UPDATE_TASK, "query" to taskRef(it.groupValues[1]), "priority" to p)
        }
        m("rename (.+?) to (.+)", s)?.let { return one(ToolIds.UPDATE_TASK, "query" to taskRef(it.groupValues[1]), "new_title" to it.groupValues[2].trim()) }
        m("add (?:a |the )?(?:subtask|sub-task|step|sub task) (.+?) (?:to|under|for) (.+)", s)?.let {
            return one(ToolIds.UPDATE_TASK, "query" to taskRef(it.groupValues[2]), "add_subtask" to it.groupValues[1].trim())
        }
        // Completion.
        m("(?:mark|set|tick|check|cross|put)(?: off)? (.+?) (?:as |off as |to |down as )?(?:completed?|done|finished)", s)?.let { return one(ToolIds.COMPLETE_TASK, "query" to taskRef(it.groupValues[1])) }
        m("(?:tick|check|cross) off (.+)", s)?.let { return one(ToolIds.COMPLETE_TASK, "query" to taskRef(it.groupValues[1])) }
        m("(?:i(?:'ve| have)? (?:already |just )?(?:finished|completed|done|did)|i(?:'m| am) done with|done with|finished|completed|complete) (.+)", s)?.let {
            return one(ToolIds.COMPLETE_TASK, "query" to taskRef(it.groupValues[1]))
        }
        m("(.+?) (?:is|are|has been) (?:done|finished|completed|complete)", s)?.let {
            val ref = it.groupValues[1]
            if (m("what|which|who|how|when|where|why|everything|nothing", ref.trim()) == null) return one(ToolIds.COMPLETE_TASK, "query" to taskRef(ref))
        }
        if (m("(?:it'?s |that'?s |this is )(?:done|finished|completed)|i (?:did|finished|completed) (?:it|that)|done it|mark (?:it|that) done", s) != null) return one(ToolIds.COMPLETE_TASK, "query" to "")
        // Postpone / snooze / reschedule.
        m("(?:postpone|snooze|delay|push back|push|move|reschedule|defer|shift)\\b\\s*(.*)", s)?.let {
            val body = it.groupValues[1].trim()
            if (m("(?:the |my )?(?:alarm|meeting|call|appointment|event)\\b.*", body) != null && !has("reminder|task", body)) return null
            val split = m("(.*?)\\s*\\b(by|for|to|until|till)\\s+(.+)", body)
            if (split != null) {
                val ref = taskRef(split.groupValues[1])
                val amount = split.groupValues[3].trim()
                val kw = split.groupValues[2].lowercase()
                val duration = if (kw == "by" || kw == "for") DateTimeParser.parseDuration(amount) else null
                return if (duration != null) one(ToolIds.POSTPONE_TASK, "query" to ref, "minutes" to maxOf(1L, duration.toMinutes()))
                else one(ToolIds.POSTPONE_TASK, "query" to ref, "by" to amount)
            }
            val duration = DateTimeParser.parseDuration(body)
            if (duration != null) {
                val ref = taskRef(rx("\\b(?:\\d+|an?|one|two|three|four|five|ten|fifteen|twenty|thirty|half an?)\\s*(?:seconds?|minutes?|mins?|hours?|hrs?|days?|weeks?)\\b.*$").replace(body, ""))
                return one(ToolIds.POSTPONE_TASK, "query" to ref, "minutes" to maxOf(1L, duration.toMinutes()))
            }
            val parsed = DateTimeParser.parse(body, time.now(), defaults())
            if (parsed.dateTime != null) return one(ToolIds.POSTPONE_TASK, "query" to taskRef(parsed.remainder), "to" to parsed.dateTime.toString())
            return one(ToolIds.POSTPONE_TASK, "query" to taskRef(body))
        }
        // Deletion.
        if (m("(?:delete|remove|clear|erase|get rid of|clean up) (?:all )?(?:my |the |of my )?(?:completed|done|finished) (?:tasks|reminders|to-?dos|items)", s) != null) return one(ToolIds.DELETE_COMPLETED)
        m("(?:delete|remove|cancel|erase|get rid of|drop) (.+)", s)?.let {
            val rest = it.groupValues[1].trim()
            val mentionsTask = has("\\b(?:reminder|task|to-?do)\\b", rest)
            val pronoun = m("it|that|this|that one|this one", rest) != null
            if (mentionsTask || pronoun) return one(ToolIds.DELETE_TASK, "query" to taskRef(rest))
        }
        return null
    }

    // ------------------------------------------------------------------ memory

    private val relations =
        "dad|father|papa|mom|mum|mother|mama|brother|sister|wife|husband|boss|girlfriend|boyfriend|best friend|friend|roommate|uncle|aunt|aunty|grandma|grandmother|grandpa|grandfather|son|daughter|manager|teacher|professor|doctor|landlord|partner|cousin"

    private fun memory(s: String): LocalIntent? {
        m("(?:save|remember|set|mark|store|add) (?:this|my current|the current|current|my) (?:place|location|spot|position) as (?:my )?(.+)", s)?.let { return one(ToolIds.SAVE_PLACE, "name" to it.groupValues[1].trim()) }
        m("(?:this is|i'm at|i am at|i'm in|i am in) (?:my )?(home|college|work|office|school|gym|hostel|university|campus)", s)?.let { return one(ToolIds.SAVE_PLACE, "name" to it.groupValues[1].trim()) }
        m("(?:call me|my name is|you can call me|refer to me as|address me as|my name's) (.+)", s)?.let { return one(ToolIds.SET_PREFERENCE, "key" to "preferred_name", "value" to it.groupValues[1].trim()) }
        m("(?:my (?:preferred |favou?rite |default )?music (?:app|player) is|(?:use|i prefer|i use|i like|set) (?=.+ (?:for|as))) ?(.+?)(?: for (?:my |playing )?(?:music|songs)| as (?:my |the )?(?:default |preferred )?music (?:app|player))?", s)?.let {
            if (has("music", s)) return one(ToolIds.SET_PREFERENCE, "key" to "music_app", "value" to it.groupValues[1].trim())
        }
        m("when i say (.+?),? i mean (.+)", s)?.let { return one(ToolIds.SET_ALIAS, "alias" to it.groupValues[1].trim(), "name" to it.groupValues[2].trim()) }
        m("(.+?) is my ($relations)", s)?.let {
            val name = it.groupValues[1].trim()
            if (m("who|what|where|how|this|that|it|today|tomorrow|he|she", name) == null && name.split(' ').size <= 4) {
                return one(ToolIds.SET_ALIAS, "alias" to it.groupValues[2].trim(), "name" to name)
            }
        }
        m("my ($relations)(?:'s)? (?:name|contact name|contact) is (.+)", s)?.let { return one(ToolIds.SET_ALIAS, "alias" to it.groupValues[1].trim(), "name" to it.groupValues[2].trim()) }
        m("(?:my )?($relations) is (?:saved as |called |named )?(.+?) in my (?:contacts|phone ?book|phone)", s)?.let { return one(ToolIds.SET_ALIAS, "alias" to it.groupValues[1].trim(), "name" to it.groupValues[2].trim()) }
        m("my ($relations) is (?:called|named|saved as) (.+)", s)?.let { return one(ToolIds.SET_ALIAS, "alias" to it.groupValues[1].trim(), "name" to it.groupValues[2].trim()) }

        m("(?:take|make|create|write|add|jot)(?: down)? (?:a |this |another )?(?:quick )?note(?: down)?(?: saying| that| to self| to myself)?[,:]?\\s+(.+)", s)?.let { return one(ToolIds.REMEMBER, "content" to it.groupValues[1].trim(), "category" to "note") }
        m("note(?: down)?(?: that)?[,:]?\\s+(.+)", s)?.let { return one(ToolIds.REMEMBER, "content" to it.groupValues[1].trim(), "category" to "note") }
        m("(?:remember|keep in mind|don't forget|do not forget|memori[sz]e|make a note)(?: that| this)?[,:]?\\s+(.+)", s)?.let {
            return one(ToolIds.REMEMBER, "content" to it.groupValues[1].trim())
        }
        if (m("remember(?: this| something)?|make a note|take a note", s) != null) return one(ToolIds.REMEMBER)
        m("(?:forget|delete|remove|erase)(?: (?:about|that|what i (?:said|told you) about|the memory (?:about|of)|everything about|what you know about))+ (.+)", s)?.let {
            return one(ToolIds.FORGET, "query" to it.groupValues[1].trim())
        }
        m("forget (?:my|our) (.+)", s)?.let { return one(ToolIds.FORGET, "query" to it.groupValues[1].trim()) }
        return null
    }

    // ------------------------------------------------------------------ calls

    private fun calls(s: String): LocalIntent? {
        if (m("call back|redial|call (?:my |the )?(?:last |latest |most recent )?missed call(?:er)?|call (?:my |the )?last call(?:er)?|call (?:back )?(?:the )?(?:last|previous) (?:person|number|caller|one)?(?: (?:who|that) called(?: me)?)?|return (?:the|my) (?:last |missed )?call|call the (?:last )?person who (?:last )?called me(?: last)?|call (?:him|her|them) back", s) != null) {
            return one(ToolIds.CALL, "last_caller" to true)
        }
        m("(?:find|get|show(?: me)?|what(?:'s| is)|tell me|look up|search for|give me) (.+?)(?:'s|s') (?:phone |mobile |contact |cell )?number", s)?.let { return one(ToolIds.FIND_CONTACT, "name" to it.groupValues[1].trim()) }
        m("(?:what(?:'s| is) |find |get )?the (?:phone |mobile |contact )?number (?:of|for) (.+)", s)?.let { return one(ToolIds.FIND_CONTACT, "name" to it.groupValues[1].trim()) }
        m("give (.+?) a (?:call|ring)", s)?.let { return one(ToolIds.CALL, "contact" to it.groupValues[1].trim()) }
        m("(?:make a |place a |start a )?(?:(whatsapp) )?(?:call|dial|phone|ring)(?: (?:to|up))? (.+?)(?: (?:on|using|via|through|with|over) (?:the )?(whatsapp|phone|sim|cellular|mobile|speaker|speakerphone))?", s)?.let {
            val who = it.groupValues[2].trim()
            if (m("me|it a day|it off|it quits", who) != null) return null
            val via = if (it.groupValues[1].isNotBlank() || it.groupValues[3].equals("whatsapp", true)) "whatsapp" else "phone"
            val digits = who.filter { c -> c.isDigit() }
            return if (digits.length >= 3 && m("[+\\d][\\d\\s()-]*", who) != null) one(ToolIds.CALL, "number" to who, "via" to via) else one(ToolIds.CALL, "contact" to who, "via" to via)
        }
        return null
    }

    // ------------------------------------------------------------------ media

    private val musicApps = "spotify|youtube music|yt music|youtube|apple music|gaana|jiosaavn|jio saavn|saavn|wynk|amazon music|vlc|soundcloud|deezer|tidal"

    private fun media(s: String): LocalIntent? {
        m("(?:open|launch|start|run|go to|switch to|bring up|fire up)\\s+(.+?)\\s+and\\s+(?:play|start playing|put on)\\s+(.+)", s)?.let {
            return one(ToolIds.PLAY_MUSIC, "query" to cleanQuery(it.groupValues[2]), "app" to it.groupValues[1].trim())
        }
        if (m("(?:next|skip)(?: (?:the |this |to the next )?(?:song|track|one|episode))?|(?:play |go to |skip to )(?:the )?next(?: (?:song|track|one|episode))?|next one", s) != null) return one(ToolIds.MEDIA_CONTROL, "action" to "next")
        if (m("(?:previous|prev)(?: (?:song|track|one))?|last (?:song|track)|(?:play |go to |go back to |skip to )(?:the )?(?:previous|last)(?: (?:song|track|one))?|go back (?:a|one) (?:song|track)|play that again", s) != null) return one(ToolIds.MEDIA_CONTROL, "action" to "previous")
        if (m("(?:pause|hold)(?: (?:the |my |this )?(?:music|song|playback|video|track|audio|podcast|it))?|stop (?:the |my |this )?(?:music|song|playback|audio|video|track|playing|podcast)", s) != null) return one(ToolIds.MEDIA_CONTROL, "action" to "pause")
        if (m("play(?: (?:some|the|my))? music|resume(?: (?:the |my )?(?:music|playback|song|video|podcast))?|continue(?: playing| (?:the )?(?:music|song))|play|unpause|keep playing", s) != null) return one(ToolIds.MEDIA_CONTROL, "action" to "play")
        if (m("what(?:'s| is) (?:playing|this song|the name of this song|that song|this track)(?: (?:right )?now)?|(?:what|which) song is (?:this|playing|that)|what am i listening to|who (?:sings|is singing) this", s) != null) return one(ToolIds.NOW_PLAYING)

        m("(?:turn |set |put )?(?:the )?(?:media |music )?volume (up|down)|turn (?:it|the (?:volume|music|sound)) (up|down)|(louder|quieter|softer)|(?:make it|a bit|a little|go) (louder|quieter|softer)", s)?.let {
            val w = it.groupValues.drop(1).first { g -> g.isNotBlank() }.lowercase()
            return one(ToolIds.SET_VOLUME, "direction" to if (w == "up" || w == "louder") "up" else "down")
        }
        m("(increase|raise|decrease|lower|reduce) (?:the )?(?:media |music )?volume", s)?.let {
            val w = it.groupValues[1].lowercase()
            return one(ToolIds.SET_VOLUME, "direction" to if (w == "increase" || w == "raise") "up" else "down")
        }
        m("(?:set |turn |put |change |make )?(?:the )?(?:media |music )?volume (?:to |at )?(\\d{1,3})\\s*(?:%|percent)?", s)?.let { return one(ToolIds.SET_VOLUME, "percent" to it.groupValues[1].toInt().coerceIn(0, 100)) }
        if (m("mute|mute (?:the )?(?:volume|music|sound|media|phone)|volume (?:off|zero)", s) != null) return one(ToolIds.SET_VOLUME, "direction" to "mute")
        if (m("unmute|unmute (?:the )?(?:volume|music|sound|media|phone)", s) != null) return one(ToolIds.SET_VOLUME, "direction" to "unmute")
        if (m("max(?:imum)? volume|full volume|volume (?:max|full|to (?:the )?max(?:imum)?)|turn (?:it|the volume) all the way up", s) != null) return one(ToolIds.SET_VOLUME, "percent" to 100)

        m("play\\s+(.+?)\\s+(?:on|in|using|with|from|via)\\s+($musicApps)", s)?.let {
            return one(ToolIds.PLAY_MUSIC, "query" to cleanQuery(it.groupValues[1]), "app" to it.groupValues[2].trim())
        }
        m("(?:play|put on|listen to)\\s+(.+)", s)?.let { return one(ToolIds.PLAY_MUSIC, "query" to cleanQuery(it.groupValues[1])) }
        return null
    }

    private fun cleanQuery(q: String): String = rx("^(?:the song|the album|the artist|some|me some|me)\\s+").replace(q.trim(), "").trim()

    // ------------------------------------------------------------------ utilities

    private fun utilities(s: String): LocalIntent? {
        m("(?:navigate|directions?|take me|drive|route|get directions|get me directions|show (?:me )?(?:the )?(?:way|route|directions)|start navigation)(?: me)? (?:to|for) (.+)|how do i get to (.+)|open navigation(?: to (.+))?|(?:take|drive|get|navigate) me (home)|navigate (home)", s)?.let {
            val dest = it.groupValues.drop(1).firstOrNull { g -> g.isNotBlank() }?.trim()
            return one(ToolIds.NAVIGATE, "destination" to dest)
        }
        m("(?:set|create|add|make)(?: me)? (?:an? |the |another )?alarm(?: clock)?(?: (?:for|at|to|in))? ?(.*)|wake me(?: up)? (?:at|by|in|around) (.+)|alarm (?:for|at) (.+)", s)?.let {
            val wake = it.groupValues[2].isNotBlank()
            val text = rx("\\b(\\d{1,2}) (\\d{2})\\b").replace(it.groupValues.drop(1).firstOrNull { g -> g.isNotBlank() }?.trim().orEmpty(), "$1:$2")
            val probe = if (m("\\d{1,2}|${"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"}", text) != null) "at $text" else text
            val parsed = DateTimeParser.parse(if (has("^(?:\\d|in |at |half|quarter|noon|midnight|tomorrow|tonight)", probe)) (if (probe.first().isDigit()) "at $probe" else probe) else probe, time.now(), defaults())
            val explicitHalf = has("\\b(?:am|pm|a\\.m|p\\.m|morning|afternoon|evening|night|tonight|noon|midnight)\\b|\\d(?:am|pm)\\b|^in ", text)
            val dt = parsed.dateTime?.let { d -> if (wake && !explicitHalf && d.hour >= 12) d.minusHours(12) else d }
            return if (dt != null && parsed.hasTime) {
                one(ToolIds.SET_ALARM, "hour" to dt.hour, "minute" to dt.minute, "label" to parsed.remainder.ifBlank { null })
            } else {
                one(ToolIds.SET_ALARM)
            }
        }
        m("(?:set|start|create|begin|put)(?: me)? (?:an? |the )?(?:timer|countdown) (?:for|of|to) (.+)|(?:set|start|create) (?:an? )?(.+?) (?:timer|countdown)|timer (?:for )?(.+)|count ?down (?:from )?(.+)", s)?.let {
            val text = it.groupValues.drop(1).firstOrNull { g -> g.isNotBlank() }?.trim().orEmpty()
            val seconds = DateTimeParser.parseDuration(text)?.seconds
            return one(ToolIds.SET_TIMER, "seconds" to seconds)
        }
        m("(?:search|google|look up|search the (?:web|internet|net)|search (?:on )?google|web search|do a (?:web |google )?search|search online)(?: for| about| on)? (.+)", s)?.let {
            return one(ToolIds.WEB_SEARCH, "query" to it.groupValues[1].trim())
        }
        return null
    }

    // ------------------------------------------------------------------ screen

    private fun screen(s: String): LocalIntent? {
        if (m("what does (?:this|that|the)(?: (?:error|message|warning|dialog|popup|pop-up|screen|page|notification))? (?:mean|say)|explain (?:this|that)(?: (?:error|screen|page|message|warning))?|what(?:'s| is) (?:this|that) (?:error|warning|message)", s) != null) return one(ToolIds.READ_SCREEN, "mode" to "explain")
        if (m("summari[sz]e (?:this|the|that)(?: (?:page|screen|article|text))?|(?:give me )?(?:a )?summary of (?:this|the) (?:page|screen|article)|what(?:'s| is) on (?:my|the|this) screen|what am i looking at", s) != null) return one(ToolIds.READ_SCREEN, "mode" to "summarize")
        if (m("read (?:this|the|my) (?:screen|page|article)(?: (?:aloud|out loud|to me|out))?|read this(?: (?:aloud|out loud|to me|out))?|read (?:it|this) out", s) != null) return one(ToolIds.READ_SCREEN, "mode" to "read")
        m("find (?:the |a |an |any )?(phone number|number|email|email address|e-mail|address|link|url|website)s? (?:on|in|from) (?:this|the|my) (?:screen|page)", s)?.let {
            return one(ToolIds.READ_SCREEN, "mode" to "find", "find" to it.groupValues[1].lowercase())
        }
        m("scroll (down|up|left|right)(?: a (?:bit|little)| more| again)?|(?:page|swipe) (down|up)", s)?.let {
            val dir = (it.groupValues[1] + it.groupValues[2]).lowercase()
            return one(ToolIds.SCREEN_ACTION, "action" to "scroll_$dir")
        }
        if (m("go back(?: and try the other (?:option|one))?|back|press back|previous (?:screen|page)", s) != null) return one(ToolIds.SCREEN_ACTION, "action" to "back")
        if (m("go home|go to (?:the )?home ?screen|press home|home ?screen", s) != null) return one(ToolIds.SCREEN_ACTION, "action" to "home")
        m("(?:click|tap|press|select|choose|hit|pick|open)(?: on)? (?:the )?(first|second|third|fourth|fifth|last|\\d+)(?:st|nd|rd|th)? (?:option|item|button|one|result|link|entry|choice|row)", s)?.let {
            return one(ToolIds.SCREEN_ACTION, "action" to "click", "ordinal" to it.groupValues[1].lowercase())
        }
        m("(?:click|tap|press|select|choose|hit)(?: on)? (?:the )?(.+?)(?: button| option| link| tab| icon)?", s)?.let {
            return one(ToolIds.SCREEN_ACTION, "action" to "click", "target" to it.groupValues[1].trim())
        }
        return null
    }

    // ------------------------------------------------------------------ apps

    private fun apps(s: String): LocalIntent? {
        if (m("(?:open|show|go to|launch)(?: the| my)? (?:android |phone |system |device )?settings(?: app)?", s) != null) return one(ToolIds.OPEN_SETTINGS)
        m("(?:open|go to|show|launch)(?: the| my)? (wi-?fi|bluetooth|display|sound|volume|battery|notification|notifications|app|apps|location|accessibility|assistant|default apps|date|time|network|internet|mobile data|data|storage|security|privacy) settings", s)?.let {
            return one(ToolIds.OPEN_SETTINGS, "panel" to it.groupValues[1].lowercase())
        }
        m("(?:open|launch|start|run|go to|switch to|bring up|fire up)(?: the| my)? (.+?)(?: app| application)?", s)?.let {
            return one(ToolIds.OPEN_APP, "name" to it.groupValues[1].trim())
        }
        return null
    }

    /** "I need to submit the form by Friday" becomes a task, but only when a time makes the intent clear. */
    private fun looseTask(s: String): LocalIntent? {
        m("i (?:need|have|want|got|should|must|ought)(?: got)? to (.+)", s)?.let {
            val parsed = DateTimeParser.parse(it.groupValues[1], time.now(), defaults())
            if (parsed.found && parsed.remainder.isNotBlank()) return reminderBody(it.groupValues[1], task = true, important = false)
        }
        return null
    }

    companion object {
        private const val YES =
            "yes|yeah|yep|yup|ya|sure|ok|okay|alright|all right|do it|go ahead|go for it|please do|confirm|confirmed|correct|right|that's right|absolutely|definitely|of course|affirmative|sounds good|yes please|yes do it|yes go ahead|proceed|yes it is|yeah sure|sure thing|fine"
        private const val NO =
            "no|nope|nah|don't|do not|cancel|cancel it|stop|never ?mind|negative|not now|no thanks|no thank you|forget it|abort|wait no|no don't|no cancel|no way|not really|don't do it"
    }
}
