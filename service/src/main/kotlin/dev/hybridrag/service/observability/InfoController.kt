package dev.hybridrag.service.observability

import com.fasterxml.jackson.databind.JsonNode
import dev.hybridrag.service.api.PipelineDefaults
import dev.hybridrag.service.config.RagProperties
import dev.hybridrag.service.llm.EmbeddingClient
import dev.hybridrag.service.llm.LlmClient
import dev.hybridrag.service.rerank.RerankerClient
import net.logstash.logback.argument.StructuredArguments.kv
import org.slf4j.LoggerFactory
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.boot.context.event.ApplicationReadyEvent
import org.springframework.context.event.EventListener
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.stereotype.Component
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController
import java.util.concurrent.atomic.AtomicReference

data class DigestStatus(
    val embeddingDigest: String?,
    val llmDigest: String?,
    val indexDigest: String?,
) {
    val matches: Boolean get() = embeddingDigest != null && embeddingDigest == indexDigest
}

/**
 * Compares the live embedding-model digest (`GET /api/tags`) with `index_metadata.embedding_model_digest` at startup
 * and on every `/v1/info` call; a mismatch is logged at ERROR and never fails the service (contract C3).
 */
@Component
class DigestCheck(
    private val embedding: EmbeddingClient,
    private val llm: LlmClient,
    private val properties: RagProperties,
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
) {
    private val logger = LoggerFactory.getLogger(javaClass)
    private val last = AtomicReference<DigestStatus?>(null)

    @EventListener(ApplicationReadyEvent::class)
    fun onStartup() {
        runCatching { check() }.onFailure { logger.warn("embedding_digest_check_failed: {}", it.message) }
    }

    fun check(index: Map<String, String> = readIndex()): DigestStatus {
        val tags = runCatching { embedding.tags() }.getOrNull()
        val status =
            DigestStatus(
                embeddingDigest = digest(tags, properties.embed.model),
                llmDigest = if (llm.provider == "ollama") digest(tags, llm.model) else null,
                indexDigest = index["embedding_model_digest"],
            )
        val previous = last.getAndSet(status)
        if (status.embeddingDigest != null && !status.matches && previous?.embeddingDigest != status.embeddingDigest) {
            logger.error(
                "embedding_digest_mismatch",
                kv("event", "embedding_digest_mismatch"),
                kv("model", properties.embed.model),
                kv("live_digest", status.embeddingDigest),
                kv("index_digest", status.indexDigest),
            )
        }
        return status
    }

    fun readIndex(): Map<String, String> =
        jdbc
            .query("select key, value from index_metadata order by key", emptyMap<String, Any>()) { row, _ ->
                row.getString("key") to row.getString("value")
            }.toMap()

    private fun digest(
        tags: JsonNode?,
        model: String,
    ): String? {
        val wanted = if (model.contains(':')) model else "$model:latest"
        return tags
            ?.path("models")
            ?.firstOrNull { it.path("name").asText() == wanted || it.path("model").asText() == wanted }
            ?.path("digest")
            ?.asText()
            ?.takeIf { it.isNotBlank() }
    }
}

/** `GET /v1/info` (contract C6); `index` is read from `index_metadata` on every call. */
@RestController
class InfoController(
    private val properties: RagProperties,
    private val llm: LlmClient,
    private val reranker: RerankerClient,
    private val digests: DigestCheck,
) {
    @GetMapping("/v1/info")
    fun info(): Map<String, Any?> {
        val index = digests.readIndex()
        val status = digests.check(index)
        val health = runCatching { reranker.health() }.getOrNull()
        return linkedMapOf(
            "service" to linkedMapOf("version" to properties.version, "git_sha" to properties.gitSha),
            "models" to
                linkedMapOf(
                    "embedding" to linkedMapOf("name" to properties.embed.model, "digest" to status.embeddingDigest),
                    "llm" to linkedMapOf("provider" to llm.provider, "name" to llm.model, "digest" to status.llmDigest),
                    "reranker" to
                        linkedMapOf(
                            "url" to properties.reranker.url,
                            "reachable" to (health != null),
                            "model" to health?.path("model")?.textValue(),
                            "revision" to health?.path("revision")?.textValue(),
                        ),
                ),
            "index" to index,
            "embedding_digest_matches_index" to status.matches,
            "exact_vector_search" to properties.retrieval.exactVectorSearch,
            "defaults" to PipelineDefaults.PIPELINE,
        )
    }
}
