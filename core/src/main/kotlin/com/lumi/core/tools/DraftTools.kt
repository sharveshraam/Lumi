package com.lumi.core.tools

import com.lumi.core.ai.ToolParam

/** Who a message is going to, as resolved by the Android layer. */
sealed class Recipient {
    data class Found(val name: String, val address: String?, val app: String?) : Recipient()
    data class Ambiguous(val names: List<String>) : Recipient()
    object NotFound : Recipient()
    data class Unavailable(val reason: String, val suggestScreen: String? = null) : Recipient()
}

data class ReplyTarget(val key: String, val sender: String, val appLabel: String)

/** Android-side messaging operations used by the dictation workflow. */
interface MessagingBackend {
    suspend fun resolveRecipient(name: String, app: String?): Recipient
    /** The notification to reply to: the one with [key], or the most recent message notification. */
    suspend fun replyTarget(key: String?): ReplyTarget?
    /** Delivers the draft, or opens the right app with it filled in. Must report what really happened. */
    suspend fun send(draft: Draft): ToolResult
}

/** Rewrites text according to an instruction ("more polite"). Returns null when unavailable. */
interface TextReviser {
    suspend fun revise(text: String, instruction: String): String?
}

/** Turns spoken punctuation into written punctuation and tidies capitalisation. */
object Dictation {
    private val replacements = listOf(
        "\\s*\\b(?:new paragraph|next paragraph)\\b\\s*" to "\n\n",
        "\\s*\\b(?:new line|next line|line break)\\b\\s*" to "\n",
        "\\s*\\b(?:full stop|period)\\b" to ".",
        "\\s*\\bquestion mark\\b" to "?",
        "\\s*\\bexclamation (?:mark|point)\\b" to "!",
        "\\s*\\bcomma\\b" to ",",
        "\\s*\\bsemicolon\\b" to ";",
        "\\s*\\bcolon\\b" to ":",
    ).map { Regex(it.first, RegexOption.IGNORE_CASE) to it.second }

    fun format(spoken: String): String {
        var s = spoken.trim()
        for ((rx, to) in replacements) s = rx.replace(s, to)
        s = s.replace(Regex("[ \\t]+([,.;:!?])"), "$1").replace(Regex("[ \\t]{2,}"), " ")
        val out = StringBuilder(s.length)
        var capitalise = true
        for (c in s) {
            if (capitalise && c.isLetter()) {
                out.append(c.uppercaseChar())
                capitalise = false
            } else {
                out.append(c)
                if (c == '.' || c == '!' || c == '?' || c == '\n') capitalise = true
                else if (!c.isWhitespace()) capitalise = false
            }
        }
        return out.toString().replace(Regex("\\bi\\b"), "I").replace(Regex("\\bi'(m|ll|ve|d)\\b"), "I'$1").trim()
    }

    /** Small deterministic rewrites used when no AI provider is configured. */
    fun basicRevise(text: String, instruction: String): String? {
        val i = instruction.lowercase()
        return when {
            "polite" in i || "politer" in i || "nicer" in i || "formal" in i -> {
                var t = text.trim()
                t = Regex("\\bcan you\\b", RegexOption.IGNORE_CASE).replace(t, "could you please")
                t = Regex("\\bi want\\b", RegexOption.IGNORE_CASE).replace(t, "I would like")
                t = Regex("\\bsend me\\b", RegexOption.IGNORE_CASE).replace(t, "please send me")
                if (!Regex("^(hi|hello|hey|dear|good (morning|afternoon|evening))\\b", RegexOption.IGNORE_CASE).containsMatchIn(t)) {
                    t = "Hi, " + t.replaceFirstChar { c -> if (t.startsWith("I ") || t.startsWith("I'")) c else c.lowercaseChar() }
                }
                if (t.last() !in ".!?") t += "."
                if (!Regex("thank", RegexOption.IGNORE_CASE).containsMatchIn(t)) t += " Thank you."
                t
            }
            "grammar" in i || "punctuation" in i || "spelling" in i -> format(text).let { if (it.last() in ".!?") it else "$it." }
            else -> null
        }
    }
}

/**
 * Universal dictation workflow: capture, read back, edit by voice, and deliver only after the
 * user explicitly confirms. Sending is never exposed to the AI planner.
 */
class DraftTools(
    private val session: SessionState,
    private val backend: MessagingBackend,
    private val reviser: TextReviser,
) {
    fun all(): List<Tool> = listOf(draftReply, draftMessage, draftEmail, typeText, edit, revise, read, send, discard)

    private fun describe(d: Draft): String = when (d.kind) {
        Draft.Kind.REPLY -> "Your reply to ${d.recipientName ?: "them"} says: \"${d.text}\"."
        Draft.Kind.MESSAGE -> "Your ${appWord(d.app)}message to ${d.recipientName ?: "them"} says: \"${d.text}\"."
        Draft.Kind.EMAIL -> "Your email to ${d.recipientName ?: "them"}" + (d.subject?.let { ", subject \"$it\"," } ?: "") + " says: \"${d.text}\"."
        Draft.Kind.TYPE -> "I'll type: \"${d.text}\"."
    }

    private fun appWord(app: String?): String = when (app?.lowercase()) {
        null, "", "sms", "text" -> ""
        else -> app.replaceFirstChar { it.uppercase() } + " "
    }

    private fun readBack(d: Draft, prefix: String = ""): ToolResult {
        session.draft = d
        val action = if (d.kind == Draft.Kind.TYPE) "Say type it to enter it" else "Say send it to send"
        return ToolResult(true, "$prefix${describe(d)} $action, or tell me what to change.", expectReply = true)
    }

    private val draftReply = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.DRAFT_REPLY,
            description = "Draft a reply to the most recently read message notification. The draft is read back; the user must say 'send it' to send.",
            params = listOf(ToolParam("text", "string", "The reply text.", required = true, prompt = "What should the reply say?")),
            permissions = listOf("special:notification_access"),
            verification = "Nothing is sent by this tool; it only stores the draft and reads it back.",
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview {
            backend.replyTarget(session.lastNotificationKey) ?: return Preview.Fail("I don't see a message to reply to. Ask me to read your latest message first.")
            return if (args.string("text") == null) Preview.Ask("text", "What should the reply say?") else Preview.Proceed
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val target = backend.replyTarget(session.lastNotificationKey) ?: return ToolResult.fail("I don't see a message to reply to. Ask me to read your latest message first.")
            val text = args.string("text") ?: return ToolResult(false, "What should the reply say?", expectReply = true)
            return readBack(Draft(Draft.Kind.REPLY, Dictation.format(text), recipientName = target.sender, app = target.appLabel, replyKey = target.key))
        }
    }

    private val draftMessage = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.DRAFT_MESSAGE,
            description = "Draft a new message to a contact (SMS by default, or WhatsApp). The draft is read back; the user must say 'send it' to send.",
            params = listOf(
                ToolParam("to", "string", "Contact name or nickname.", required = true, prompt = "Who is the message for?"),
                ToolParam("text", "string", "The message text.", required = true, prompt = "What should the message say?"),
                ToolParam("app", "string", "Messaging app.", enumValues = listOf("sms", "whatsapp")),
            ),
            permissions = listOf("android.permission.READ_CONTACTS"),
            verification = "Nothing is sent by this tool; it only stores the draft and reads it back.",
        )

        override suspend fun preview(args: Args, env: TurnEnv): Preview {
            val to = args.string("to") ?: return Preview.Ask("to", "Who is the message for?")
            return when (val r = backend.resolveRecipient(to, args.string("app"))) {
                is Recipient.Found -> if (args.string("text") == null) Preview.Ask("text", "What should the message say?") else Preview.Proceed
                is Recipient.Ambiguous -> Preview.Ask("to", "I found ${r.names.take(4).joinToString(", ")}. Which one?")
                Recipient.NotFound -> Preview.Fail("I couldn't find \"$to\" in your contacts.")
                is Recipient.Unavailable -> Preview.Fail(r.reason, r.suggestScreen)
            }
        }

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val to = args.string("to") ?: return ToolResult(false, "Who is the message for?", expectReply = true)
            val text = args.string("text") ?: return ToolResult(false, "What should the message say?", expectReply = true)
            return when (val r = backend.resolveRecipient(to, args.string("app"))) {
                is Recipient.Found -> readBack(Draft(Draft.Kind.MESSAGE, Dictation.format(text), recipientName = r.name, recipientAddress = r.address, app = r.app ?: args.string("app") ?: "sms"))
                is Recipient.Ambiguous -> ToolResult(false, "I found ${r.names.take(4).joinToString(", ")}. Which one?", expectReply = true)
                Recipient.NotFound -> ToolResult.fail("I couldn't find \"$to\" in your contacts.")
                is Recipient.Unavailable -> ToolResult.fail(r.reason, r.suggestScreen)
            }
        }
    }

    private val draftEmail = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.DRAFT_EMAIL,
            description = "Draft an email. It is read back, then opened in the user's email app when they say 'send it'.",
            params = listOf(
                ToolParam("to", "string", "Recipient name or email address.", required = true, prompt = "Who is the email for?"),
                ToolParam("subject", "string", "Subject line."),
                ToolParam("body", "string", "Email body.", required = true, prompt = "What should the email say?"),
            ),
            verification = "Nothing is sent by this tool; it only stores the draft and reads it back.",
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val to = args.string("to") ?: return ToolResult(false, "Who is the email for?", expectReply = true)
            val body = args.string("body") ?: return ToolResult(false, "What should the email say?", expectReply = true)
            val address = to.takeIf { it.contains('@') }?.replace(" ", "")
            return readBack(Draft(Draft.Kind.EMAIL, Dictation.format(body), recipientName = to, recipientAddress = address, subject = args.string("subject")?.let { Dictation.format(it) }))
        }
    }

    private val typeText = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.TYPE_TEXT,
            description = "Dictate text into the text field currently focused on screen. Read back first; entered when the user says 'type it'.",
            params = listOf(ToolParam("text", "string", "The text to type.", required = true, prompt = "What should I type?")),
            permissions = listOf("special:accessibility"),
            verification = "Nothing is typed by this tool; it only stores the draft and reads it back.",
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val text = args.string("text") ?: return ToolResult(false, "What should I type?", expectReply = true)
            return readBack(Draft(Draft.Kind.TYPE, Dictation.format(text)))
        }
    }

    private fun noDraft() = ToolResult.fail("There's no draft at the moment.")

    private val edit = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.EDIT_DRAFT,
            description = "Replace or extend the text of the current draft.",
            params = listOf(
                ToolParam("text", "string", "New text, or text to append.", required = true, prompt = "What should it say?"),
                ToolParam("mode", "string", "replace (default) or append.", enumValues = listOf("replace", "append")),
            ),
            allowedWhenLocked = true,
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val d = session.draft ?: return noDraft()
            val text = Dictation.format(args.string("text") ?: return ToolResult(false, "What should it say?", expectReply = true))
            val updated = if (args.string("mode") == "append") {
                val joiner = if (d.text.isNotEmpty() && d.text.last() in ".!?\n") " " else if (d.text.isEmpty()) "" else ". "
                d.copy(text = d.text + joiner + text)
            } else {
                d.copy(text = text)
            }
            return readBack(updated, "Updated. ")
        }
    }

    private val revise = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.REVISE_DRAFT,
            description = "Rewrite the current draft according to an instruction such as 'more polite' or 'shorter'.",
            params = listOf(ToolParam("instruction", "string", "How to change the draft.", required = true, prompt = "How should I change it?")),
            allowedWhenLocked = true,
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val d = session.draft ?: return noDraft()
            val instruction = args.string("instruction") ?: return ToolResult(false, "How should I change it?", expectReply = true)
            val revised = reviser.revise(d.text, instruction)?.trim()?.trim('"')?.takeIf { it.isNotEmpty() }
                ?: Dictation.basicRevise(d.text, instruction)
                ?: return ToolResult(false, "I need an AI provider to rewrite it like that. You can also just tell me the new wording: say \"change it to\" and the text.", expectReply = true, suggestScreen = "settings")
            return readBack(d.copy(text = revised), "Here's the new version. ")
        }
    }

    private val read = object : Tool {
        override val spec = ToolSpec(ToolIds.READ_DRAFT, "Read the current draft back to the user.", allowedWhenLocked = true)

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val d = session.draft ?: return noDraft()
            return readBack(d)
        }
    }

    private val send = object : Tool {
        override val spec = ToolSpec(
            id = ToolIds.SEND_DRAFT,
            description = "Send the current draft. Only triggered by the user's explicit spoken confirmation.",
            exposeToAi = false,
            verification = "Reports whether the message was actually delivered, or only opened in the target app for the user to send.",
        )

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            val d = session.draft ?: return noDraft()
            val result = backend.send(d)
            if (result.success) session.draft = null
            return result
        }
    }

    private val discard = object : Tool {
        override val spec = ToolSpec(ToolIds.DISCARD_DRAFT, "Discard the current draft without sending.", allowedWhenLocked = true)

        override suspend fun execute(args: Args, env: TurnEnv): ToolResult {
            if (session.draft == null) return noDraft()
            session.draft = null
            return ToolResult.ok("Okay, I've discarded it. Nothing was sent.", verified = true)
        }
    }
}
