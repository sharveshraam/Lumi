package com.lumi.core.ai

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

data class HttpRequestSpec(val url: String, val headers: Map<String, String>, val body: String)

/** Pure translation between Lumi's chat model and each provider's HTTP API. */
object WireFormats {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    fun buildRequest(config: AiConfig, messages: List<ChatMessage>, tools: List<ToolDef>): HttpRequestSpec = when (config.type) {
        ProviderType.OPENAI_COMPATIBLE -> openAiRequest(config, messages, tools)
        ProviderType.ANTHROPIC -> anthropicRequest(config, messages, tools)
        ProviderType.GEMINI -> geminiRequest(config, messages, tools)
    }

    fun parseResponse(type: ProviderType, body: String): AiResult = try {
        val root = json.parseToJsonElement(body) as? JsonObject
            ?: return AiResult.Error(AiErrorKind.BAD_RESPONSE, "The AI service returned an unexpected response.")
        when (type) {
            ProviderType.OPENAI_COMPATIBLE -> parseOpenAi(root)
            ProviderType.ANTHROPIC -> parseAnthropic(root)
            ProviderType.GEMINI -> parseGemini(root)
        }
    } catch (e: Exception) {
        AiResult.Error(AiErrorKind.BAD_RESPONSE, "The AI service returned a response I couldn't read.")
    }

    /** Extracts a human-readable message from a provider error body. */
    fun errorMessage(body: String): String? = try {
        val root = json.parseToJsonElement(body) as? JsonObject
        val err = root?.get("error")
        when (err) {
            is JsonObject -> err.str("message")
            is JsonPrimitive -> err.contentOrNullSafe()
            else -> root?.str("message")
        }
    } catch (e: Exception) {
        null
    }

    // ------------------------------------------------------------------ schema

    private fun schema(tool: ToolDef, upperCaseTypes: Boolean): JsonObject = buildJsonObject {
        put("type", if (upperCaseTypes) "OBJECT" else "object")
        putJsonObject("properties") {
            for (p in tool.params) {
                putJsonObject(p.name) {
                    put("type", if (upperCaseTypes) p.type.uppercase() else p.type)
                    put("description", p.description)
                    if (p.enumValues != null) putJsonArray("enum") { p.enumValues.forEach { add(it) } }
                }
            }
        }
        val required = tool.params.filter { it.required }
        if (required.isNotEmpty()) putJsonArray("required") { required.forEach { add(it.name) } }
    }

    // ------------------------------------------------------------------ OpenAI-compatible

    private fun openAiRequest(config: AiConfig, messages: List<ChatMessage>, tools: List<ToolDef>): HttpRequestSpec {
        val body = buildJsonObject {
            put("model", config.model)
            putJsonArray("messages") {
                for (m in messages) addJsonObject {
                    put("role", m.role.name.lowercase())
                    when (m.role) {
                        Role.TOOL -> {
                            put("tool_call_id", m.toolCallId ?: "")
                            put("content", m.content)
                        }
                        Role.ASSISTANT -> {
                            if (m.content.isEmpty() && m.toolCalls.isNotEmpty()) put("content", JsonNull) else put("content", m.content)
                            if (m.toolCalls.isNotEmpty()) putJsonArray("tool_calls") {
                                for (c in m.toolCalls) addJsonObject {
                                    put("id", c.id)
                                    put("type", "function")
                                    putJsonObject("function") {
                                        put("name", c.name)
                                        put("arguments", c.args.toString())
                                    }
                                }
                            }
                        }
                        else -> put("content", m.content)
                    }
                }
            }
            if (tools.isNotEmpty()) putJsonArray("tools") {
                for (t in tools) addJsonObject {
                    put("type", "function")
                    putJsonObject("function") {
                        put("name", t.name)
                        put("description", t.description)
                        put("parameters", schema(t, upperCaseTypes = false))
                    }
                }
            }
        }
        return HttpRequestSpec(
            config.baseUrl.trimEnd('/') + "/chat/completions",
            mapOf("Authorization" to "Bearer ${config.apiKey}", "Content-Type" to "application/json"),
            body.toString(),
        )
    }

    private fun parseOpenAi(root: JsonObject): AiResult {
        val message = (root["choices"] as? JsonArray)?.firstOrNull().obj()?.get("message").obj()
            ?: return AiResult.Error(AiErrorKind.BAD_RESPONSE, errorMessage(root.toString()) ?: "The AI service returned no message.")
        val calls = (message["tool_calls"] as? JsonArray).orEmpty().mapIndexedNotNull { i, el ->
            val call = el.obj() ?: return@mapIndexedNotNull null
            val fn = call["function"].obj() ?: return@mapIndexedNotNull null
            val name = fn.str("name") ?: return@mapIndexedNotNull null
            ToolCall(call.str("id") ?: "call_$i", name, parseArgs(fn["arguments"]))
        }
        return AiResult.Ok(message.str("content").orEmpty().trim(), calls)
    }

    // ------------------------------------------------------------------ Anthropic

    private fun anthropicRequest(config: AiConfig, messages: List<ChatMessage>, tools: List<ToolDef>): HttpRequestSpec {
        val system = messages.filter { it.role == Role.SYSTEM }.joinToString("\n\n") { it.content }
        val body = buildJsonObject {
            put("model", config.model)
            put("max_tokens", config.maxTokens)
            if (system.isNotEmpty()) put("system", system)
            putJsonArray("messages") {
                var i = 0
                val rest = messages.filter { it.role != Role.SYSTEM }
                while (i < rest.size) {
                    val m = rest[i]
                    when (m.role) {
                        Role.TOOL -> {
                            // Consecutive tool results belong in a single user turn.
                            val group = mutableListOf<ChatMessage>()
                            while (i < rest.size && rest[i].role == Role.TOOL) group += rest[i++]
                            addJsonObject {
                                put("role", "user")
                                putJsonArray("content") {
                                    for (g in group) addJsonObject {
                                        put("type", "tool_result")
                                        put("tool_use_id", g.toolCallId ?: "")
                                        put("content", g.content)
                                    }
                                }
                            }
                            continue
                        }
                        Role.ASSISTANT -> addJsonObject {
                            put("role", "assistant")
                            putJsonArray("content") {
                                if (m.content.isNotEmpty()) addJsonObject { put("type", "text"); put("text", m.content) }
                                for (c in m.toolCalls) addJsonObject {
                                    put("type", "tool_use")
                                    put("id", c.id)
                                    put("name", c.name)
                                    put("input", c.args)
                                }
                            }
                        }
                        else -> addJsonObject {
                            put("role", "user")
                            put("content", m.content)
                        }
                    }
                    i++
                }
            }
            if (tools.isNotEmpty()) putJsonArray("tools") {
                for (t in tools) addJsonObject {
                    put("name", t.name)
                    put("description", t.description)
                    put("input_schema", schema(t, upperCaseTypes = false))
                }
            }
        }
        return HttpRequestSpec(
            config.baseUrl.trimEnd('/') + "/v1/messages",
            mapOf("x-api-key" to config.apiKey, "anthropic-version" to "2023-06-01", "Content-Type" to "application/json"),
            body.toString(),
        )
    }

    private fun parseAnthropic(root: JsonObject): AiResult {
        val content = root["content"] as? JsonArray
            ?: return AiResult.Error(AiErrorKind.BAD_RESPONSE, errorMessage(root.toString()) ?: "The AI service returned no content.")
        val text = StringBuilder()
        val calls = mutableListOf<ToolCall>()
        for ((i, el) in content.withIndex()) {
            val block = el.obj() ?: continue
            when (block.str("type")) {
                "text" -> text.append(block.str("text").orEmpty())
                "tool_use" -> {
                    val name = block.str("name") ?: continue
                    calls += ToolCall(block.str("id") ?: "call_$i", name, block["input"].obj() ?: JsonObject(emptyMap()))
                }
            }
        }
        return AiResult.Ok(text.toString().trim(), calls)
    }

    // ------------------------------------------------------------------ Gemini

    private fun geminiRequest(config: AiConfig, messages: List<ChatMessage>, tools: List<ToolDef>): HttpRequestSpec {
        val system = messages.filter { it.role == Role.SYSTEM }.joinToString("\n\n") { it.content }
        val body = buildJsonObject {
            if (system.isNotEmpty()) putJsonObject("systemInstruction") {
                putJsonArray("parts") { addJsonObject { put("text", system) } }
            }
            putJsonArray("contents") {
                var i = 0
                val rest = messages.filter { it.role != Role.SYSTEM }
                while (i < rest.size) {
                    val m = rest[i]
                    when (m.role) {
                        Role.TOOL -> {
                            val group = mutableListOf<ChatMessage>()
                            while (i < rest.size && rest[i].role == Role.TOOL) group += rest[i++]
                            addJsonObject {
                                put("role", "user")
                                putJsonArray("parts") {
                                    for (g in group) addJsonObject {
                                        putJsonObject("functionResponse") {
                                            put("name", g.toolName ?: "")
                                            putJsonObject("response") { put("result", g.content) }
                                        }
                                    }
                                }
                            }
                            continue
                        }
                        Role.ASSISTANT -> addJsonObject {
                            put("role", "model")
                            putJsonArray("parts") {
                                if (m.content.isNotEmpty()) addJsonObject { put("text", m.content) }
                                for (c in m.toolCalls) addJsonObject {
                                    putJsonObject("functionCall") {
                                        put("name", c.name)
                                        put("args", c.args)
                                    }
                                    if (c.signature != null) put("thoughtSignature", c.signature)
                                }
                            }
                        }
                        else -> addJsonObject {
                            put("role", "user")
                            putJsonArray("parts") { addJsonObject { put("text", m.content) } }
                        }
                    }
                    i++
                }
            }
            if (tools.isNotEmpty()) putJsonArray("tools") {
                addJsonObject {
                    putJsonArray("functionDeclarations") {
                        for (t in tools) addJsonObject {
                            put("name", t.name)
                            put("description", t.description)
                            if (t.params.isNotEmpty()) put("parameters", schema(t, upperCaseTypes = true))
                        }
                    }
                }
            }
        }
        return HttpRequestSpec(
            config.baseUrl.trimEnd('/') + "/v1beta/models/${config.model}:generateContent",
            mapOf("x-goog-api-key" to config.apiKey, "Content-Type" to "application/json"),
            body.toString(),
        )
    }

    private fun parseGemini(root: JsonObject): AiResult {
        val parts = (root["candidates"] as? JsonArray)?.firstOrNull().obj()?.get("content").obj()?.get("parts") as? JsonArray
            ?: return AiResult.Error(AiErrorKind.BAD_RESPONSE, errorMessage(root.toString()) ?: "The AI service returned no content.")
        val text = StringBuilder()
        val calls = mutableListOf<ToolCall>()
        for ((i, el) in parts.withIndex()) {
            val part = el.obj() ?: continue
            if (part.str("thought") == "true") continue
            part.str("text")?.let { text.append(it) }
            val fc = part["functionCall"].obj() ?: continue
            val name = fc.str("name") ?: continue
            calls += ToolCall("call_$i", name, fc["args"].obj() ?: JsonObject(emptyMap()), part.str("thoughtSignature"))
        }
        return AiResult.Ok(text.toString().trim(), calls)
    }

    // ------------------------------------------------------------------ helpers

    private fun parseArgs(el: JsonElement?): JsonObject = when (el) {
        is JsonObject -> el
        is JsonPrimitive -> try {
            json.parseToJsonElement(el.content.ifBlank { "{}" }) as? JsonObject ?: JsonObject(emptyMap())
        } catch (e: Exception) {
            JsonObject(emptyMap())
        }
        else -> JsonObject(emptyMap())
    }

    private fun JsonElement?.obj(): JsonObject? = this as? JsonObject
    private fun JsonPrimitive.contentOrNullSafe(): String? = if (this is JsonNull) null else content
    private fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.contentOrNullSafe()
}
