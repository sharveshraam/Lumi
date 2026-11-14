package com.lumi.core

import com.lumi.core.ai.AiConfig
import com.lumi.core.ai.AiErrorKind
import com.lumi.core.ai.AiResult
import com.lumi.core.ai.ChatMessage
import com.lumi.core.ai.HttpAiClient
import com.lumi.core.ai.HttpResponse
import com.lumi.core.ai.HttpTransport
import com.lumi.core.ai.ProviderType
import com.lumi.core.ai.Role
import com.lumi.core.ai.ToolCall
import com.lumi.core.ai.ToolDef
import com.lumi.core.ai.ToolParam
import com.lumi.core.ai.WireFormats
import com.lumi.core.learning.LearningEngine
import com.lumi.core.memory.MemoryService
import com.lumi.core.memory.RememberResult
import com.lumi.core.model.MemoryCategory
import com.lumi.core.model.SubtaskCodec
import com.lumi.core.model.Subtask
import com.lumi.core.tools.Args
import com.lumi.core.tools.Dictation
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class MemoryAndAiTest {
    private val time = FakeTime()
    private val policy = FakePolicy()
    private val store = InMemoryMemoryStore()
    private val memory = MemoryService(store, policy, time)

    // ------------------------------------------------------------------ memory

    @Test fun keyedMemoriesAreReplacedNotDuplicated(): Unit = runBlocking {
        memory.remember(MemoryCategory.PREFERENCE, "Sam", MemoryService.KEY_NAME)
        memory.remember(MemoryCategory.PREFERENCE, "Samuel", MemoryService.KEY_NAME)
        assertEquals(1, store.items.size)
        assertEquals("Samuel", memory.get(MemoryCategory.PREFERENCE, MemoryService.KEY_NAME))
    }

    @Test fun disabledCategoryStoresNothingAndIsHidden(): Unit = runBlocking {
        memory.remember(MemoryCategory.ROUTINE, "Gym at 6", "gym")
        policy.disabled += MemoryCategory.ROUTINE
        assertTrue(memory.remember(MemoryCategory.ROUTINE, "Class at 9", "class") is RememberResult.CategoryDisabled)
        assertEquals(1, store.items.size)
        assertTrue(memory.byCategory(MemoryCategory.ROUTINE).isEmpty())
        assertTrue(memory.contextSnippets().isEmpty())
        assertNull(memory.get(MemoryCategory.ROUTINE, "gym"))
    }

    @Test fun learnedFactsRespectTheLearningSwitchButExplicitOnesDoNot(): Unit = runBlocking {
        policy.learning = false
        assertNull(memory.learn(MemoryCategory.APP_PREFERENCE, "Spotify", MemoryService.KEY_MUSIC_APP))
        assertTrue(store.items.isEmpty())
        assertTrue(memory.remember(MemoryCategory.EXPLICIT, "My locker code is 4521") is RememberResult.Saved)
    }

    @Test fun aliasesResolveCaseInsensitively(): Unit = runBlocking {
        memory.setAlias("Mom", "Amma Home")
        assertEquals("Amma Home", memory.resolveAlias("mom"))
        assertEquals("Amma Home", memory.resolveAlias("my mom"))
        assertNull(memory.resolveAlias("dad"))
    }

    @Test fun searchForgetAndClear(): Unit = runBlocking {
        memory.remember(MemoryCategory.EXPLICIT, "My locker code is 4521")
        memory.remember(MemoryCategory.EXPLICIT, "The wifi password is on the fridge")
        memory.remember(MemoryCategory.NOTE, "Idea: build a bird feeder")
        val hit = memory.search("locker code").single()
        assertTrue(hit.content.contains("4521"))
        memory.forget(hit.id)
        assertTrue(memory.search("locker").isEmpty())
        memory.clear(MemoryCategory.EXPLICIT)
        assertEquals(listOf(MemoryCategory.NOTE), store.items.map { it.category })
    }

    @Test fun patternIsOfferedOnceAtTheThreshold(): Unit = runBlocking {
        val engine = LearningEngine(InMemoryPatterns(), policy, time)
        assertNull(engine.observeReminder("Water the plants"))
        assertNull(engine.observeReminder("water the plants"))
        val offer = engine.observeReminder("Water the plants!")
        assertNotNull(offer)
        assertNull(engine.observeReminder("water the plants"))
        engine.resolve(offer!!.key, approved = false)
        assertNull(engine.observeReminder("water the plants"))
    }

    @Test fun subtasksSurviveEncoding() {
        val items = listOf(Subtask("Write intro; with | odd, chars", true), Subtask("Add \"references\" section"))
        assertEquals(items, SubtaskCodec.decode(SubtaskCodec.encode(items)))
        assertTrue(SubtaskCodec.decode(null).isEmpty())
        assertTrue(SubtaskCodec.decode("").isEmpty())
    }

    // ------------------------------------------------------------------ dictation

    @Test fun spokenPunctuationBecomesWritten() {
        assertEquals("Hi Alex, are you free today? Call me.", Dictation.format("hi Alex comma are you free today question mark call me full stop"))
        assertEquals("First line\nSecond line", Dictation.format("first line new line second line"))
        assertEquals("I think I'm late", Dictation.format("i think i'm late"))
    }

    @Test fun offlinePoliteRewriteOnlyHandlesWhatItCan() {
        val polite = Dictation.basicRevise("can you send me the notes", "make it more polite")!!
        assertTrue(polite, polite.contains("please"))
        assertNull(Dictation.basicRevise("hello", "translate to french"))
    }

    // ------------------------------------------------------------------ provider wire formats

    private val tools = listOf(ToolDef("create_task", "Create a task", listOf(ToolParam("title", "string", "Title", required = true), ToolParam("when", "string", "When"))))
    private val convo = listOf(
        ChatMessage(Role.SYSTEM, "You are Lumi."),
        ChatMessage(Role.USER, "remind me"),
        ChatMessage(Role.ASSISTANT, "", listOf(ToolCall("call_1", "create_task", Args.of("title" to "x")))),
        ChatMessage(Role.TOOL, "OK: done", toolCallId = "call_1", toolName = "create_task"),
    )

    private fun obj(text: String) = Json.parseToJsonElement(text).jsonObject

    @Test fun openAiRequestAndResponse() {
        val req = WireFormats.buildRequest(AiConfig(ProviderType.OPENAI_COMPATIBLE, "https://api.example.com/v1/", "m1", "KEY"), convo, tools)
        assertEquals("https://api.example.com/v1/chat/completions", req.url)
        assertEquals("Bearer KEY", req.headers["Authorization"])
        val body = obj(req.body)
        assertEquals("m1", body["model"]!!.jsonPrimitive.content)
        val messages = body["messages"]!!.jsonArray
        assertEquals(4, messages.size)
        assertEquals("tool", messages[3].jsonObject["role"]!!.jsonPrimitive.content)
        assertEquals("call_1", messages[3].jsonObject["tool_call_id"]!!.jsonPrimitive.content)
        assertEquals("create_task", body["tools"]!!.jsonArray[0].jsonObject["function"]!!.jsonObject["name"]!!.jsonPrimitive.content)
        assertFalse(req.body.contains("KEY"))

        val parsed = WireFormats.parseResponse(
            ProviderType.OPENAI_COMPATIBLE,
            """{"choices":[{"message":{"role":"assistant","content":null,"tool_calls":[{"id":"a1","type":"function","function":{"name":"create_task","arguments":"{\"title\":\"Buy milk\",\"when\":\"tomorrow\"}"}}]}}]}""",
        ) as AiResult.Ok
        assertEquals("create_task", parsed.toolCalls.single().name)
        assertEquals("Buy milk", Args(parsed.toolCalls.single().args).string("title"))
        val text = WireFormats.parseResponse(ProviderType.OPENAI_COMPATIBLE, """{"choices":[{"message":{"content":"Hello"}}]}""") as AiResult.Ok
        assertEquals("Hello", text.text)
    }

    @Test fun anthropicRequestAndResponse() {
        val req = WireFormats.buildRequest(AiConfig(ProviderType.ANTHROPIC, "https://api.anthropic.com", "claude", "KEY"), convo, tools)
        assertEquals("https://api.anthropic.com/v1/messages", req.url)
        assertEquals("KEY", req.headers["x-api-key"])
        val body = obj(req.body)
        assertEquals("You are Lumi.", body["system"]!!.jsonPrimitive.content)
        val messages = body["messages"]!!.jsonArray
        assertEquals("user", messages[0].jsonObject["role"]!!.jsonPrimitive.content)
        assertEquals("tool_use", messages[1].jsonObject["content"]!!.jsonArray.last().jsonObject["type"]!!.jsonPrimitive.content)
        assertEquals("tool_result", messages[2].jsonObject["content"]!!.jsonArray[0].jsonObject["type"]!!.jsonPrimitive.content)
        assertNotNull(body["tools"]!!.jsonArray[0].jsonObject["input_schema"])

        val parsed = WireFormats.parseResponse(
            ProviderType.ANTHROPIC,
            """{"content":[{"type":"text","text":"On it."},{"type":"tool_use","id":"t1","name":"create_task","input":{"title":"Buy milk"}}]}""",
        ) as AiResult.Ok
        assertEquals("On it.", parsed.text)
        assertEquals("t1", parsed.toolCalls.single().id)
    }

    @Test fun geminiRequestAndResponse() {
        val req = WireFormats.buildRequest(AiConfig(ProviderType.GEMINI, "https://generativelanguage.googleapis.com/v1beta", "gemini-x", "KEY"), convo, tools)
        assertTrue(req.url, req.url.endsWith("/models/gemini-x:generateContent"))
        assertEquals("KEY", req.headers["x-goog-api-key"])
        assertFalse("the key must not be in the URL", req.url.contains("KEY"))
        val body = obj(req.body)
        assertNotNull(body["systemInstruction"])
        val contents = body["contents"]!!.jsonArray
        assertEquals("model", contents[1].jsonObject["role"]!!.jsonPrimitive.content)
        assertNotNull(contents[1].jsonObject["parts"]!!.jsonArray[0].jsonObject["functionCall"])
        assertNotNull(contents[2].jsonObject["parts"]!!.jsonArray[0].jsonObject["functionResponse"])

        val parsed = WireFormats.parseResponse(
            ProviderType.GEMINI,
            """{"candidates":[{"content":{"parts":[{"functionCall":{"name":"create_task","args":{"title":"Buy milk"}}}]}}]}""",
        ) as AiResult.Ok
        assertEquals("create_task", parsed.toolCalls.single().name)
    }

    @Test fun malformedResponsesBecomeErrorsNotCrashes() {
        for (type in ProviderType.values()) {
            assertTrue(WireFormats.parseResponse(type, "not json") is AiResult.Error)
            assertTrue(WireFormats.parseResponse(type, "[]") is AiResult.Error)
            assertNotNull(WireFormats.parseResponse(type, "{}"))
        }
        assertEquals("Invalid key", WireFormats.errorMessage("""{"error":{"message":"Invalid key"}}"""))
        assertNull(WireFormats.errorMessage("<html>"))
    }

    private fun client(config: AiConfig, respond: () -> HttpResponse) = HttpAiClient(
        config,
        object : HttpTransport {
            override fun post(url: String, headers: Map<String, String>, body: String): HttpResponse = respond()
        },
        Dispatchers.Unconfined,
    )

    @Test fun httpClientMapsFailures(): Unit = runBlocking {
        val cfg = AiConfig(ProviderType.OPENAI_COMPATIBLE, "https://api.example.com/v1", "m", "KEY")
        val user = listOf(ChatMessage(Role.USER, "hi"))
        fun kind(r: AiResult) = (r as AiResult.Error).kind
        assertEquals(AiErrorKind.AUTH, kind(client(cfg) { HttpResponse(401, "{}") }.chat(user, emptyList())))
        assertEquals(AiErrorKind.RATE_LIMIT, kind(client(cfg) { HttpResponse(429, "{}") }.chat(user, emptyList())))
        assertEquals(AiErrorKind.BAD_RESPONSE, kind(client(cfg) { HttpResponse(500, "oops") }.chat(user, emptyList())))
        assertEquals(AiErrorKind.NETWORK, kind(client(cfg) { throw IOException("down") }.chat(user, emptyList())))
        assertEquals(AiErrorKind.NOT_CONFIGURED, kind(client(cfg.copy(apiKey = "")) { HttpResponse(200, "{}") }.chat(user, emptyList())))
        assertEquals(AiErrorKind.NOT_CONFIGURED, kind(client(cfg.copy(baseUrl = "http://insecure.example.com")) { HttpResponse(200, "{}") }.chat(user, emptyList())))
        val ok = client(cfg) { HttpResponse(200, """{"choices":[{"message":{"content":"Hello"}}]}""") }.chat(user, emptyList())
        assertEquals("Hello", (ok as AiResult.Ok).text)
    }
}
