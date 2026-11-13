package com.lumi.core.learning

import com.lumi.core.memory.MemoryPolicy
import com.lumi.core.memory.PatternStore
import com.lumi.core.model.PatternRecord
import com.lumi.core.model.PatternStatus
import com.lumi.core.model.TimeSource

/** Something Lumi noticed and wants to ask the user about. Never applied without consent. */
data class PatternOffer(val key: String, val kind: Kind, val subject: String, val question: String) {
    enum class Kind { RECURRING_REMINDER, MUSIC_APP }
}

/**
 * Counts repeated behaviour and, after [THRESHOLD] sightings, produces a single question for the
 * user. A declined pattern is never offered again; nothing is created silently.
 */
class LearningEngine(
    private val patterns: PatternStore,
    private val policy: MemoryPolicy,
    private val time: TimeSource,
) {
    suspend fun observeReminder(title: String): PatternOffer? {
        val norm = normalise(title)
        if (norm.isEmpty()) return null
        val display = title.trim()
        return observe("reminder:$norm", display, PatternOffer.Kind.RECURRING_REMINDER) {
            "By the way, you've asked me about \"$display\" $THRESHOLD times now. Want me to make it a recurring reminder?"
        }
    }

    suspend fun observeMusicApp(appName: String, alreadyPreferred: Boolean): PatternOffer? {
        if (alreadyPreferred || appName.isBlank()) return null
        val display = appName.trim()
        return observe("music_app:${display.lowercase()}", display, PatternOffer.Kind.MUSIC_APP) {
            "I've noticed you usually play music with $display. Should I use it as your default music app?"
        }
    }

    private suspend fun observe(key: String, label: String, kind: PatternOffer.Kind, question: () -> String): PatternOffer? {
        if (!policy.learningEnabled()) return null
        val now = time.nowMillis()
        val existing = patterns.get(key)
        if (existing != null && (existing.status == PatternStatus.DECLINED || existing.status == PatternStatus.APPROVED)) return null
        val count = (existing?.count ?: 0) + 1
        val shouldOffer = count >= THRESHOLD && existing?.status != PatternStatus.OFFERED
        patterns.put(PatternRecord(key, label, count, if (shouldOffer) PatternStatus.OFFERED else existing?.status ?: PatternStatus.OBSERVING, now))
        return if (shouldOffer) PatternOffer(key, kind, label, question()) else null
    }

    suspend fun resolve(key: String, approved: Boolean) {
        val existing = patterns.get(key) ?: return
        patterns.put(existing.copy(status = if (approved) PatternStatus.APPROVED else PatternStatus.DECLINED, lastSeenAt = time.nowMillis()))
    }

    companion object {
        const val THRESHOLD = 3
        private val FILLER = setOf("my", "the", "a", "an", "to", "about", "for", "please")
        fun normalise(title: String): String =
            title.lowercase().split(Regex("[^\\p{L}\\p{N}]+")).filter { it.isNotEmpty() && it !in FILLER }.joinToString(" ")
    }
}
