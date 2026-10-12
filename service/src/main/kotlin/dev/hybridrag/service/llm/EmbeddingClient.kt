package dev.hybridrag.service.llm

import com.fasterxml.jackson.databind.JsonNode
import dev.hybridrag.service.config.CallType
import dev.hybridrag.service.config.HttpClients
import dev.hybridrag.service.config.RagProperties
import dev.hybridrag.service.config.getJson
import dev.hybridrag.service.config.postJson
import dev.hybridrag.service.resilience.Dependency
import dev.hybridrag.service.resilience.DependencyGuards
import dev.hybridrag.service.resilience.MalformedResponseException
import org.springframework.stereotype.Component

data class Embedding(
    val vector: FloatArray,
    val promptTokens: Int,
)

/** Ollama `/api/embed` with exactly one input (contract C3); always Ollama, whatever the LLM provider. */
@Component
class EmbeddingClient(
    private val properties: RagProperties,
    private val http: HttpClients,
    private val guards: DependencyGuards,
) {
    fun embed(text: String): Embedding {
        val json =
            dependencyMapper.writeValueAsString(
                linkedMapOf(
                    "model" to properties.embed.model,
                    "input" to listOf(text),
                    "truncate" to true,
                    "keep_alive" to properties.ollama.keepAlive,
                ),
            )
        return guards.call(Dependency.EMBEDDING) {
            val body = http.get(CallType.EMBED).postJson(Dependency.EMBEDDING, properties.ollama.url + "/api/embed", json)
            val node = parseBody(Dependency.EMBEDDING, body)
            val values = node.path("embeddings").path(0)
            if (!values.isArray || values.size() != 1024) {
                throw MalformedResponseException(Dependency.EMBEDDING, "expected one 1024-d embedding")
            }
            Embedding(FloatArray(1024) { values[it].floatValue() }, node.path("prompt_eval_count").asInt(0))
        }
    }

    /** `GET /api/tags` with the 1 s info timeout and no breaker (design D10). */
    fun tags(): JsonNode = parseBody(Dependency.LLM, http.get(CallType.INFO).getJson(Dependency.LLM, properties.ollama.url + "/api/tags"))
}
