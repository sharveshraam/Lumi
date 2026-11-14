package com.lumi.core.ai

import kotlinx.serialization.json.JsonObject

enum class Role { SYSTEM, USER, ASSISTANT, TOOL }

/** A structured request from the model (or the local parser) to run one tool. */
data class ToolCall(
    val id: String,
    val name: String,
    val args: JsonObject,
    /** Opaque provider token that must be echoed back with the call (used by Gemini). */
    val signature: String? = null,
)

data class ChatMessage(
    val role: Role,
    val content: String = "",
    val toolCalls: List<ToolCall> = emptyList(),
    val toolCallId: String? = null,
    val toolName: String? = null,
)

data class ToolParam(
    val name: String,
    /** JSON schema primitive: string, integer, number or boolean. */
    val type: String,
    val description: String,
    val required: Boolean = false,
    val enumValues: List<String>? = null,
    /** Question to ask the user when a required value is missing. */
    val prompt: String? = null,
)

data class ToolDef(val name: String, val description: String, val params: List<ToolParam>)

enum class AiErrorKind { NOT_CONFIGURED, NETWORK, AUTH, RATE_LIMIT, BAD_RESPONSE }

sealed class AiResult {
    data class Ok(val text: String, val toolCalls: List<ToolCall>) : AiResult()
    data class Error(val kind: AiErrorKind, val message: String) : AiResult()
}

/** Provider-independent chat interface. Implementations must not throw for network failures. */
interface AiClient {
    suspend fun chat(messages: List<ChatMessage>, tools: List<ToolDef>): AiResult
}

enum class ProviderType { OPENAI_COMPATIBLE, ANTHROPIC, GEMINI }

data class AiConfig(
    val type: ProviderType,
    val baseUrl: String,
    val model: String,
    val apiKey: String,
    val maxTokens: Int = 1024,
)

/** Starting points shown in settings. Base URL and model stay editable because providers change them. */
data class AiPreset(val id: String, val label: String, val type: ProviderType, val baseUrl: String, val model: String)

object AiPresets {
    val all: List<AiPreset> = listOf(
        AiPreset("openai", "OpenAI", ProviderType.OPENAI_COMPATIBLE, "https://api.openai.com/v1", "gpt-4o-mini"),
        AiPreset("anthropic", "Anthropic Claude", ProviderType.ANTHROPIC, "https://api.anthropic.com", "claude-3-5-haiku-latest"),
        AiPreset("gemini", "Google Gemini", ProviderType.GEMINI, "https://generativelanguage.googleapis.com", "gemini-2.0-flash"),
        AiPreset("openrouter", "OpenRouter", ProviderType.OPENAI_COMPATIBLE, "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
        AiPreset("groq", "Groq", ProviderType.OPENAI_COMPATIBLE, "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        AiPreset("custom", "Custom OpenAI-compatible", ProviderType.OPENAI_COMPATIBLE, "", ""),
    )

    fun byId(id: String?): AiPreset? = all.firstOrNull { it.id == id }
}
