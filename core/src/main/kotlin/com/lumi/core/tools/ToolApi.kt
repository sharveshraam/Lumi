package com.lumi.core.tools

import com.lumi.core.ai.ToolDef
import com.lumi.core.ai.ToolParam
import com.lumi.core.learning.PatternOffer
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** When a tool needs the user's go-ahead before it runs. */
enum class Confirmation {
    NEVER,
    /** Always ask (destructive or outward-facing actions). */
    ALWAYS,
    /** Ask unless the user switched confirmation off for this tool in settings. */
    IF_ENABLED,
}

/**
 * Declarative description of one action Lumi can take. The same spec drives the AI tool
 * schema, permission checks, confirmation and lock-screen policy.
 */
data class ToolSpec(
    val id: String,
    val description: String,
    val params: List<ToolParam> = emptyList(),
    /** Android permission names (or Lumi special-access ids) that must be granted first. */
    val permissions: List<String> = emptyList(),
    val confirmation: Confirmation = Confirmation.NEVER,
    /** Whether the tool may run while the device is locked. Defaults to no. */
    val allowedWhenLocked: Boolean = false,
    /** How the result is verified, for documentation and the debug screen. */
    val verification: String = "Reports the result returned by the underlying operation.",
    /** True for look-ups whose result an AI planner may need to reason over before answering. */
    val query: Boolean = false,
    /** True when the result contains third-party content that must not be sent to an online AI. */
    val privateOutput: Boolean = false,
    /** False for actions that only an explicit user utterance may trigger (e.g. sending a draft). */
    val exposeToAi: Boolean = true,
) {
    fun toDef(): ToolDef = ToolDef(id, description, params)
}

/** Follow-up behaviour the assistant surface should perform after speaking. */
sealed class Directive {
    object StopSpeaking : Directive()
    object Dismiss : Directive()
    object Repeat : Directive()
    /** -1 slower, 0 normal, +1 faster. */
    data class SpeechRate(val change: Int) : Directive()
    /** Open a full-screen page of the main app ("tasks", "memory", "permissions", "settings", "home"). */
    data class OpenScreen(val screen: String) : Directive()
    object RequestUnlock : Directive()
}

data class ToolResult(
    val success: Boolean,
    /** What Lumi says. Must describe what actually happened. */
    val message: String,
    /** True when the outcome was checked, false when it could not be, null when not applicable. */
    val verified: Boolean? = null,
    /** Keep listening for a reply after speaking. */
    val expectReply: Boolean = false,
    val directive: Directive? = null,
    val offer: PatternOffer? = null,
    /** Screen that would help the user fix a failure (e.g. "permissions"). */
    val suggestScreen: String? = null,
) {
    companion object {
        fun ok(message: String, verified: Boolean? = null, expectReply: Boolean = false) = ToolResult(true, message, verified, expectReply)
        fun fail(message: String, suggestScreen: String? = null) = ToolResult(false, message, suggestScreen = suggestScreen)
    }
}

/** Outcome of the pre-flight check that runs before confirmation and execution. */
sealed class Preview {
    object Proceed : Preview()
    /** Ask the user to confirm; [args] may carry resolved arguments to execute with. */
    data class Confirm(val prompt: String, val args: JsonObject? = null) : Preview()
    /** A value is missing or ambiguous; the user's answer fills [param]. */
    data class Ask(val param: String, val prompt: String) : Preview()
    data class Fail(val message: String, val suggestScreen: String? = null) : Preview()
}

/** Conditions of the current turn. */
data class TurnEnv(val deviceLocked: Boolean = false, val voice: Boolean = true)

/** Typed access to tool arguments, tolerant of the loose JSON that language models produce. */
class Args(val json: JsonObject) {
    fun string(name: String): String? {
        val el = json[name] ?: return null
        if (el is JsonNull) return null
        return (el as? JsonPrimitive)?.content?.trim()?.takeIf { it.isNotEmpty() }
    }

    fun int(name: String): Int? = string(name)?.toDoubleOrNull()?.toInt()
    fun long(name: String): Long? = string(name)?.toDoubleOrNull()?.toLong()
    fun double(name: String): Double? = string(name)?.toDoubleOrNull()
    fun bool(name: String): Boolean? = when (string(name)?.lowercase()) {
        "true", "yes", "1" -> true
        "false", "no", "0" -> false
        else -> null
    }

    fun with(name: String, value: String): JsonObject = buildJsonObject {
        for ((k, v) in json) if (k != name) put(k, v)
        put(name, value)
    }

    companion object {
        fun of(vararg pairs: Pair<String, Any?>): JsonObject = buildJsonObject {
            for ((k, v) in pairs) when (v) {
                null -> {}
                is Boolean -> put(k, v)
                is Number -> put(k, v)
                else -> put(k, v.toString())
            }
        }
    }
}

interface Tool {
    val spec: ToolSpec

    /** Cheap validation and resolution before the user is asked to confirm. Must not have side effects. */
    suspend fun preview(args: Args, env: TurnEnv): Preview = Preview.Proceed

    suspend fun execute(args: Args, env: TurnEnv): ToolResult
}

class ToolRegistry(tools: List<Tool>) {
    private val byId: Map<String, Tool> = tools.associateBy { it.spec.id }

    init {
        require(byId.size == tools.size) { "Duplicate tool ids: " + tools.groupBy { it.spec.id }.filter { it.value.size > 1 }.keys }
    }

    fun get(id: String): Tool? = byId[id]
    fun all(): List<Tool> = byId.values.toList()
    fun defs(lockedOnly: Boolean = false): List<ToolDef> =
        byId.values.filter { it.spec.exposeToAi && (!lockedOnly || it.spec.allowedWhenLocked) }.map { it.spec.toDef() }
}

/** Runtime permission state, provided by the Android layer. */
interface PermissionGate {
    /** Subset of [permissions] that is not currently granted. */
    fun missing(permissions: List<String>): List<String>
    /** Short human name, e.g. "Contacts". */
    fun label(permission: String): String
}

/** A message, note or other text being composed by voice, awaiting confirmation. */
data class Draft(
    val kind: Kind,
    val text: String,
    val recipientName: String? = null,
    /** Phone number or email address when known. */
    val recipientAddress: String? = null,
    /** Target app label or package ("whatsapp", "sms", ...). */
    val app: String? = null,
    val subject: String? = null,
    /** Notification key when this is a reply to a notification. */
    val replyKey: String? = null,
) {
    enum class Kind { MESSAGE, REPLY, EMAIL, TYPE }
}

/** Conversation-scoped working memory shared by tools ("it", "that reminder", the current draft). */
class SessionState {
    @Volatile var lastTaskId: Long? = null
    @Volatile var listedTaskIds: List<Long> = emptyList()
    @Volatile var draft: Draft? = null
    @Volatile var lastNotificationKey: String? = null
    @Volatile var lastSpokenMessage: String? = null

    fun reset() {
        lastTaskId = null
        listedTaskIds = emptyList()
        draft = null
        lastNotificationKey = null
        lastSpokenMessage = null
    }
}
