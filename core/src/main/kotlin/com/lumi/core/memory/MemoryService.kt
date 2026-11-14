package com.lumi.core.memory

import com.lumi.core.model.Memory
import com.lumi.core.model.MemoryCategory
import com.lumi.core.model.PatternRecord
import com.lumi.core.model.TimeSource
import com.lumi.core.model.Turn

/** Persistence contract for personal memory (Room in the app). */
interface MemoryStore {
    suspend fun insert(memory: Memory): Long
    suspend fun update(memory: Memory)
    suspend fun delete(id: Long)
    suspend fun all(): List<Memory>
    suspend fun clearCategory(category: MemoryCategory)
}

/** User-controlled switches for what Lumi may store. */
interface MemoryPolicy {
    /** When false, nothing is inferred or stored implicitly. Explicit "remember that" still works. */
    fun learningEnabled(): Boolean
    fun categoryEnabled(category: MemoryCategory): Boolean
}

interface PatternStore {
    suspend fun get(key: String): PatternRecord?
    suspend fun put(record: PatternRecord)
    suspend fun all(): List<PatternRecord>
    suspend fun clear()
}

interface HistoryStore {
    suspend fun append(role: String, text: String)
    suspend fun recent(limit: Int): List<Turn>
    suspend fun clear()
}

sealed class RememberResult {
    data class Saved(val memory: Memory) : RememberResult()
    data class CategoryDisabled(val category: MemoryCategory) : RememberResult()
}

/** Local-first personal memory. Nothing here depends on an AI provider's conversation history. */
class MemoryService(
    private val store: MemoryStore,
    private val policy: MemoryPolicy,
    private val time: TimeSource,
) {
    /** Stores a memory. With a [key], replaces the existing value for that key. */
    suspend fun remember(category: MemoryCategory, content: String, key: String? = null, source: String = "explicit"): RememberResult {
        if (!policy.categoryEnabled(category)) return RememberResult.CategoryDisabled(category)
        val now = time.nowMillis()
        val text = content.trim()
        val existing = store.all().firstOrNull {
            it.category == category && (if (key != null) it.key == key else it.key == null && it.content.equals(text, ignoreCase = true))
        }
        if (existing != null) {
            val updated = existing.copy(content = text, updatedAt = now, source = source)
            store.update(updated)
            return RememberResult.Saved(updated)
        }
        val memory = Memory(category = category, key = key, content = text, source = source, createdAt = now, updatedAt = now)
        return RememberResult.Saved(memory.copy(id = store.insert(memory)))
    }

    /** Stores something Lumi inferred. Refused when learning is switched off. */
    suspend fun learn(category: MemoryCategory, content: String, key: String? = null): RememberResult? {
        if (!policy.learningEnabled()) return null
        return remember(category, content, key, source = "learned")
    }

    suspend fun get(category: MemoryCategory, key: String): String? {
        if (!policy.categoryEnabled(category)) return null
        return store.all().firstOrNull { it.category == category && it.key == key }?.content
    }

    suspend fun preference(key: String): String? = get(MemoryCategory.PREFERENCE, key) ?: get(MemoryCategory.APP_PREFERENCE, key)

    suspend fun byCategory(category: MemoryCategory): List<Memory> =
        if (!policy.categoryEnabled(category)) emptyList() else store.all().filter { it.category == category }.sortedByDescending { it.updatedAt }

    suspend fun all(): List<Memory> = store.all().filter { policy.categoryEnabled(it.category) }

    /** Resolves a nickname such as "dad" to the contact name the user taught Lumi. */
    suspend fun resolveAlias(name: String): String? = get(MemoryCategory.CONTACT_ALIAS, aliasKey(name))

    suspend fun setAlias(alias: String, contactName: String): RememberResult =
        remember(MemoryCategory.CONTACT_ALIAS, contactName.trim(), aliasKey(alias))

    suspend fun search(query: String): List<Memory> {
        val words = query.lowercase().split(Regex("[^\\p{L}\\p{N}]+")).filter { it.length > 2 && it !in STOP }
        if (words.isEmpty()) return emptyList()
        return all().map { m ->
            val hay = (m.content + " " + (m.key ?: "")).lowercase()
            m to words.count { hay.contains(it) }
        }.filter { it.second > 0 }.sortedByDescending { it.second }.map { it.first }
    }

    suspend fun forget(id: Long) = store.delete(id)
    suspend fun update(memory: Memory) = store.update(memory.copy(updatedAt = time.nowMillis()))
    suspend fun clear(category: MemoryCategory) = store.clearCategory(category)

    /** Short facts suitable for an AI system prompt. Only enabled categories are included. */
    suspend fun contextSnippets(limit: Int = 30): List<String> = all()
        .filter { it.category != MemoryCategory.HISTORY && it.category != MemoryCategory.NOTE }
        .sortedByDescending { it.updatedAt }
        .take(limit)
        .map { m -> if (m.key != null) "${m.key.removePrefix("alias:").replace('_', ' ')}: ${m.content}" else m.content }

    companion object {
        const val KEY_NAME = "preferred_name"
        const val KEY_MUSIC_APP = "music_app"
        private val STOP = setOf("the", "and", "about", "what", "you", "know", "remember", "that", "did", "ask", "for")
        fun aliasKey(alias: String): String = "alias:" + alias.trim().lowercase().removePrefix("my ").trim()
    }
}
