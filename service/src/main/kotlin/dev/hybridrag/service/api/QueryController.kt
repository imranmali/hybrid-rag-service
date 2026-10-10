package dev.hybridrag.service.api

import com.fasterxml.jackson.databind.JsonNode
import dev.hybridrag.service.RagProperties
import dev.hybridrag.service.llm.OllamaClient
import dev.hybridrag.service.llm.RerankerClient
import dev.hybridrag.service.observability.TraceContext
import dev.hybridrag.service.pipeline.QueryPipeline
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.http.HttpHeaders
import org.springframework.http.HttpStatus
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.http.converter.HttpMessageNotReadableException
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.web.bind.annotation.ExceptionHandler
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RestController
import org.springframework.web.bind.annotation.RestControllerAdvice

@RestController
class QueryController(
    private val pipeline: QueryPipeline,
) {
    @PostMapping("/v1/query")
    fun query(
        @RequestBody request: QueryRequest,
    ): ResponseEntity<QueryResponse> {
        val response = pipeline.query(request)
        val headers =
            HttpHeaders().apply {
                set("X-Trace-Id", response.traceId)
                if (response.degraded.isNotEmpty()) set("X-RAG-Degraded", response.degraded.joinToString(","))
            }
        return ResponseEntity.ok().headers(headers).body(response)
    }
}

@RestController
class InfoController(
    private val properties: RagProperties,
    private val ollama: OllamaClient,
    private val reranker: RerankerClient,
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
) {
    @GetMapping("/v1/info")
    fun info(): Map<String, Any?> {
        val index =
            jdbc
                .query("select key, value from index_metadata", emptyMap<String, Any>()) { row, _ ->
                    row.getString("key") to row.getString("value")
                }.toMap()
        val tags = safe { ollama.tags() }
        val rerankHealth = safe { reranker.health() }
        val embeddingDigest = digest(tags, properties.embed.model)
        val llmDigest = digest(tags, properties.llm.model)
        val expected = index["embedding_model_digest"]
        return mapOf(
            "service" to mapOf("version" to properties.version, "git_sha" to properties.gitSha),
            "models" to
                mapOf(
                    "embedding" to mapOf("name" to properties.embed.model, "digest" to embeddingDigest),
                    "llm" to mapOf("provider" to properties.llm.provider, "name" to properties.llm.model, "digest" to llmDigest),
                    "reranker" to
                        mapOf(
                            "url" to properties.reranker.url,
                            "reachable" to (rerankHealth != null),
                            "model" to rerankHealth?.path("model")?.asText(),
                            "revision" to rerankHealth?.path("revision")?.asText(),
                        ),
                ),
            "index" to index,
            "embedding_digest_matches_index" to (embeddingDigest != null && embeddingDigest == expected),
            "exact_vector_search" to properties.retrieval.exactVectorSearch,
            "defaults" to Pipeline(),
        )
    }

    private fun safe(block: () -> JsonNode): JsonNode? = runCatching(block).getOrNull()

    private fun digest(
        tags: JsonNode?,
        model: String,
    ): String? =
        tags
            ?.path("models")
            ?.firstOrNull { it.path("name").asText().removeSuffix(":latest") == model.removeSuffix(":latest") }
            ?.path("digest")
            ?.asText()
}

data class Problem(
    val type: String = "about:blank",
    val title: String,
    val status: Int,
    val detail: String,
    val errors: List<FieldError>? = null,
    val traceId: String = TraceContext.get(),
)

@RestControllerAdvice
class ProblemHandler {
    @ExceptionHandler(RequestValidationException::class)
    fun validation(error: RequestValidationException): ResponseEntity<Problem> =
        problem(HttpStatus.BAD_REQUEST, "Bad Request", error.message ?: "Invalid request", error.errors)

    @ExceptionHandler(HttpMessageNotReadableException::class)
    fun unreadable(error: HttpMessageNotReadableException): ResponseEntity<Problem> =
        problem(
            HttpStatus.BAD_REQUEST,
            "Bad Request",
            "Invalid JSON body",
            listOf(
                FieldError(
                    "body",
                    error.mostSpecificCause.message ?: "invalid JSON",
                ),
            ),
        )

    @ExceptionHandler(RetrievalUnavailableException::class)
    fun unavailable(error: RetrievalUnavailableException): ResponseEntity<Problem> =
        problem(HttpStatus.SERVICE_UNAVAILABLE, "Service Unavailable", error.message ?: "Retrieval unavailable")

    @ExceptionHandler(Exception::class)
    fun unknown(error: Exception): ResponseEntity<Problem> =
        problem(HttpStatus.INTERNAL_SERVER_ERROR, "Internal Server Error", error.message ?: "Unexpected error")

    private fun problem(
        status: HttpStatus,
        title: String,
        detail: String,
        errors: List<FieldError>? = null,
    ): ResponseEntity<Problem> {
        val traceId = TraceContext.get()
        return ResponseEntity
            .status(status)
            .header("X-Trace-Id", traceId)
            .contentType(MediaType.valueOf("application/problem+json"))
            .body(Problem(title = title, status = status.value(), detail = detail, errors = errors, traceId = traceId))
    }
}
