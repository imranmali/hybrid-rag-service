package dev.hybridrag.service.rerank

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.hybridrag.service.config.CallType
import dev.hybridrag.service.config.HttpClients
import dev.hybridrag.service.config.RagProperties
import dev.hybridrag.service.config.getJson
import dev.hybridrag.service.config.postJson
import dev.hybridrag.service.resilience.Dependency
import dev.hybridrag.service.resilience.DependencyGuards
import dev.hybridrag.service.resilience.MalformedResponseException
import org.springframework.stereotype.Component

data class RerankPassage(
    val id: Long,
    val text: String,
)

/** `POST /rerank` per contract C5; returns chunk id → score. */
@Component
class RerankerClient(
    private val properties: RagProperties,
    private val http: HttpClients,
    private val guards: DependencyGuards,
) {
    private val mapper = jacksonObjectMapper()

    fun rerank(
        query: String,
        passages: List<RerankPassage>,
    ): Map<Long, Double> {
        val json =
            mapper.writeValueAsString(
                linkedMapOf(
                    "query" to query,
                    "passages" to passages.map { linkedMapOf("id" to it.id.toString(), "text" to it.text) },
                    "top_k" to passages.size,
                ),
            )
        return guards.call(Dependency.RERANKER) {
            val body = http.get(CallType.RERANK).postJson(Dependency.RERANKER, properties.reranker.url + "/rerank", json)
            val results =
                runCatching { mapper.readTree(body).path("results") }
                    .getOrNull()
                    ?.takeIf { it.isArray }
                    ?: throw MalformedResponseException(Dependency.RERANKER, "missing results")
            results.associate { node ->
                val id = node.path("id").asText().toLongOrNull() ?: throw MalformedResponseException(Dependency.RERANKER, "bad id")
                id to node.path("score").asDouble()
            }
        }
    }

    /** `GET /healthz` with the 1 s info timeout and no breaker. */
    fun health(): JsonNode = mapper.readTree(http.get(CallType.INFO).getJson(Dependency.RERANKER, properties.reranker.url + "/healthz"))
}
