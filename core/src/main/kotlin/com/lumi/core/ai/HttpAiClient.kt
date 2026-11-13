package com.lumi.core.ai

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

data class HttpResponse(val code: Int, val body: String)

/** Minimal blocking HTTP seam so providers can be tested without a network. */
interface HttpTransport {
    @Throws(IOException::class)
    fun post(url: String, headers: Map<String, String>, body: String): HttpResponse
}

class UrlConnectionTransport(private val connectTimeoutMs: Int = 15_000, private val readTimeoutMs: Int = 60_000) : HttpTransport {
    override fun post(url: String, headers: Map<String, String>, body: String): HttpResponse {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "POST"
            connection.connectTimeout = connectTimeoutMs
            connection.readTimeout = readTimeoutMs
            connection.doOutput = true
            for ((k, v) in headers) connection.setRequestProperty(k, v)
            connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            return HttpResponse(code, text)
        } finally {
            connection.disconnect()
        }
    }
}

/** [AiClient] for any provider described by an [AiConfig]. One request per call; no retries, no polling. */
class HttpAiClient(
    private val config: AiConfig,
    private val transport: HttpTransport = UrlConnectionTransport(),
    private val dispatcher: CoroutineDispatcher = Dispatchers.IO,
) : AiClient {
    override suspend fun chat(messages: List<ChatMessage>, tools: List<ToolDef>): AiResult {
        if (config.apiKey.isBlank() || config.baseUrl.isBlank() || config.model.isBlank()) {
            return AiResult.Error(AiErrorKind.NOT_CONFIGURED, "No AI provider is configured.")
        }
        if (!config.baseUrl.startsWith("https://", ignoreCase = true)) {
            return AiResult.Error(AiErrorKind.NOT_CONFIGURED, "The AI provider URL must use https.")
        }
        val request = WireFormats.buildRequest(config, messages, tools)
        return withContext(dispatcher) {
            try {
                val response = transport.post(request.url, request.headers, request.body)
                when {
                    response.code in 200..299 -> WireFormats.parseResponse(config.type, response.body)
                    response.code == 401 || response.code == 403 ->
                        AiResult.Error(AiErrorKind.AUTH, WireFormats.errorMessage(response.body) ?: "The AI provider rejected the API key.")
                    response.code == 429 ->
                        AiResult.Error(AiErrorKind.RATE_LIMIT, WireFormats.errorMessage(response.body) ?: "The AI provider is rate limiting requests.")
                    else ->
                        AiResult.Error(AiErrorKind.BAD_RESPONSE, WireFormats.errorMessage(response.body) ?: "The AI provider returned HTTP ${response.code}.")
                }
            } catch (e: IOException) {
                AiResult.Error(AiErrorKind.NETWORK, "I couldn't reach the AI service. Check your internet connection.")
            } catch (e: SecurityException) {
                AiResult.Error(AiErrorKind.NETWORK, "Network access isn't available.")
            }
        }
    }
}
