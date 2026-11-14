package com.lumi.core.learning

import com.lumi.core.memory.MemoryService
import com.lumi.core.model.MemoryCategory

/**
 * Short, skippable, conversational setup. Every answer is stored verbatim as the user gave it;
 * nothing is invented. The user can say "skip" for any question or "stop" to end early.
 */
class OnboardingFlow(private val memory: MemoryService) {
    data class Step(val key: String, val category: MemoryCategory, val question: String, val prefix: String)

    data class Progress(val reply: String, val finished: Boolean)

    val steps: List<Step> = listOf(
        Step(MemoryService.KEY_NAME, MemoryCategory.PREFERENCE, "What should I call you?", ""),
        Step("daily_schedule", MemoryCategory.ROUTINE, "When do you usually wake up and go to sleep?", "Wake and sleep times: "),
        Step("work_timings", MemoryCategory.ROUTINE, "What are your usual college or work timings?", "College or work timings: "),
        Step("regular_activities", MemoryCategory.ROUTINE, "Any regular activities I should know about, like gym or classes?", "Regular activities: "),
        Step(MemoryService.KEY_MUSIC_APP, MemoryCategory.APP_PREFERENCE, "Which app do you prefer for music?", ""),
        Step("favourite_playlists", MemoryCategory.PREFERENCE, "Do you have favourite playlists I should know by name?", "Favourite playlists: "),
        Step("important_contacts", MemoryCategory.PREFERENCE, "Who do you contact most, and what do you call them? For example: Dad is Ramesh Kumar.", "Important contacts: "),
        Step("frequent_apps", MemoryCategory.APP_PREFERENCE, "Which apps do you use most?", "Frequently used apps: "),
        Step("projects", MemoryCategory.PREFERENCE, "Are you working on any important projects right now?", "Ongoing projects: "),
    )

    private var index = -1
    private var resumeAt = 0
    val active: Boolean get() = index in steps.indices

    /** Starts setup, or resumes where the user stopped last time. */
    fun start(): Progress {
        index = if (resumeAt in steps.indices) resumeAt else 0
        val intro = if (index == 0) "Let's get to know each other. I'll ask a few quick questions." else "Let's pick up where we left off."
        return Progress("$intro Say skip to pass on any of them, or stop to finish. ${steps[index].question}", finished = false)
    }

    fun cancel() {
        if (active) resumeAt = index
        index = -1
    }

    suspend fun answer(utterance: String): Progress {
        if (!active) return Progress("We're not in setup right now.", finished = true)
        val text = utterance.trim().trim('.', '!')
        val lower = text.lowercase()
        if (STOP.matches(lower)) {
            resumeAt = index
            index = -1
            return Progress("No problem. We can continue any time: just say \"continue setup\". I'll keep learning as we go.", finished = true)
        }
        val step = steps[index]
        var ack = "Okay, skipped."
        if (!SKIP.matches(lower) && text.isNotEmpty()) {
            ack = store(step, text)
        }
        index++
        if (index >= steps.size) {
            index = -1
            resumeAt = 0
            return Progress("$ack That's everything for now. You can review or delete anything I've learned in the Memory screen.", finished = true)
        }
        return Progress("$ack ${steps[index].question}", finished = false)
    }

    private suspend fun store(step: Step, text: String): String {
        return when (step.key) {
            MemoryService.KEY_NAME -> {
                val name = NAME_PREFIX.replace(text, "").trim().ifEmpty { text }
                memory.remember(step.category, name, step.key, source = "onboarding")
                "Nice to meet you, $name."
            }
            MemoryService.KEY_MUSIC_APP -> {
                val app = APP_PREFIX.replace(text, "").trim().ifEmpty { text }
                memory.remember(step.category, app, step.key, source = "onboarding")
                "Got it, $app for music."
            }
            "important_contacts" -> {
                memory.remember(step.category, step.prefix + text, step.key, source = "onboarding")
                var aliases = 0
                for (m in ALIAS.findAll(text)) {
                    val alias = m.groupValues[1].trim()
                    val name = m.groupValues[2].trim()
                    if (alias.isNotEmpty() && name.isNotEmpty() && alias.split(' ').size <= 3) {
                        memory.setAlias(alias, name)
                        aliases++
                    }
                }
                if (aliases > 0) "Got it. I saved $aliases nickname${if (aliases == 1) "" else "s"}." else "Got it."
            }
            else -> {
                memory.remember(step.category, step.prefix + text, step.key, source = "onboarding")
                "Got it."
            }
        }
    }

    companion object {
        private val SKIP = Regex("^(skip|skip it|skip this|skip that|pass|next|no|nope|none|nothing|not really|i don't know|don't know|no idea|prefer not to say)( one| question)?$")
        private val STOP = Regex("^(stop|cancel|quit|exit|later|that's enough|enough|finish|done|end|stop setup|not now|maybe later)$")
        private val NAME_PREFIX = Regex("^(call me|my name is|i am|i'm|it's|you can call me|just)\\s+", RegexOption.IGNORE_CASE)
        private val APP_PREFIX = Regex("^(i use|i prefer|mostly|usually|it's|i like)\\s+", RegexOption.IGNORE_CASE)
        private val ALIAS = Regex("(?:^|,|;|\\band\\b)\\s*(?:my\\s+)?([\\p{L} ]+?)\\s+is\\s+([\\p{L}. ]+?)(?=$|,|;|\\s+and\\s)", RegexOption.IGNORE_CASE)
    }
}
