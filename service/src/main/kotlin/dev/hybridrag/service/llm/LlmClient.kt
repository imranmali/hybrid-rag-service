package dev.hybridrag.service.llm

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.hybridrag.service.config.CallType
import dev.hybridrag.service.config.HttpClients
import dev.hybridrag.service.config.RagProperties
import dev.hybridrag.service.config.postJson
import dev.hybridrag.service.resilience.Dependency
import dev.hybridrag.service.resilience.DependencyGuards
import dev.hybridrag.service.resilience.MalformedResponseException
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration

/** LLM uses with their read timeout, `num_predict` (contract C3) and whether `format: json` is sent. */
enum class LlmUse(
    val callType: CallType,
    val numPredict: Int,
    val structured: Boolean,
) {
    ROUTE(CallType.LLM_ROUTE, 32, true),
    SQL_GENERATION(CallType.LLM_SQLGEN, 256, true),
    ANSWER(CallType.LLM_GENERATE, 384, false),
}

data class LlmReply(
    val content: String,
    val promptTokens: Int,
    val completionTokens: Int,
)

interface LlmClient {
    val provider: String
    val model: String

    fun chat(
        use: LlmUse,
        system: String,
        user: String,
    ): LlmReply
}

internal val dependencyMapper = jacksonObjectMapper()

internal fun parseBody(
    dependency: Dependency,
    body: String,
): JsonNode = runCatching { dependencyMapper.readTree(body) }.getOrNull() ?: throw MalformedResponseException(dependency, "not JSON")

/** Ollama `/api/chat` with the exact contract C3 body; usage from `prompt_eval_count` / `eval_count`. */
class OllamaLlmClient(
    private val properties: RagProperties,
    private val http: HttpClients,
    private val guards: DependencyGuards,
) : LlmClient {
    override val provider = "ollama"
    override val model: String get() = properties.llm.model

    override fun chat(
        use: LlmUse,
        system: String,
        user: String,
    ): LlmReply {
        val body = linkedMapOf<String, Any>()
        body["model"] = properties.llm.model
        body["messages"] = listOf(mapOf("role" to "system", "content" to system), mapOf("role" to "user", "content" to user))
        body["stream"] = false
        if (use.structured) body["format"] = "json"
        body["keep_alive"] = properties.ollama.keepAlive
        body["options"] =
            linkedMapOf("temperature" to 0, "seed" to properties.llm.seed, "num_ctx" to 8192, "num_predict" to use.numPredict)
        val json = dependencyMapper.writeValueAsString(body)
        return guards.call(Dependency.LLM) {
            val node = parseBody(Dependency.LLM, http.get(use.callType).postJson(Dependency.LLM, properties.ollama.url + "/api/chat", json))
            val content = node.path("message").path("content")
            if (!content.isTextual) throw MalformedResponseException(Dependency.LLM, "missing message.content")
            LlmReply(content.asText(), node.path("prompt_eval_count").asInt(0), node.path("eval_count").asInt(0))
        }
    }
}

/** Anthropic Messages API, used only when `rag.llm.provider=anthropic` (design D8). */
class AnthropicLlmClient(
    private val properties: RagProperties,
    private val http: HttpClients,
    private val guards: DependencyGuards,
) : LlmClient {
    override val provider = "anthropic"
    override val model: String get() = properties.anthropic.model

    override fun chat(
        use: LlmUse,
        system: String,
        user: String,
    ): LlmReply {
        val json =
            dependencyMapper.writeValueAsString(
                linkedMapOf(
                    "model" to properties.anthropic.model,
                    "max_tokens" to use.numPredict,
                    "temperature" to 0,
                    "system" to system,
                    "messages" to listOf(mapOf("role" to "user", "content" to user)),
                ),
            )
        val headers = mapOf("x-api-key" to properties.anthropic.apiKey, "anthropic-version" to "2023-06-01")
        return guards.call(Dependency.LLM) {
            val url = properties.anthropic.url.trimEnd('/') + "/v1/messages"
            val node = parseBody(Dependency.LLM, http.get(use.callType).postJson(Dependency.LLM, url, json, headers))
            val text = node.path("content").firstOrNull { it.path("type").asText() == "text" }?.path("text")
            if (text == null || !text.isTextual) throw MalformedResponseException(Dependency.LLM, "missing text content")
            LlmReply(text.asText(), node.path("usage").path("input_tokens").asInt(0), node.path("usage").path("output_tokens").asInt(0))
        }
    }
}

@Configuration
class LlmConfig {
    /** Provider selection; `anthropic` fails startup when its key or model is blank (contract C3). */
    @Bean
    fun llmClient(
        properties: RagProperties,
        http: HttpClients,
        guards: DependencyGuards,
    ): LlmClient =
        when (properties.llm.provider) {
            "ollama" -> OllamaLlmClient(properties, http, guards)
            "anthropic" -> {
                require(properties.anthropic.apiKey.isNotBlank()) {
                    "ANTHROPIC_API_KEY (rag.anthropic.api-key) must be set when rag.llm.provider=anthropic"
                }
                require(properties.anthropic.model.isNotBlank()) {
                    "ANTHROPIC_MODEL (rag.anthropic.model) must be set when rag.llm.provider=anthropic"
                }
                AnthropicLlmClient(properties, http, guards)
            }
            else -> throw IllegalArgumentException("rag.llm.provider must be ollama or anthropic, got '${properties.llm.provider}'")
        }
}
