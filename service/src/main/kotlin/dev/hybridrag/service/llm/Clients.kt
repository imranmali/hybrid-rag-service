package dev.hybridrag.service.llm

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import dev.hybridrag.service.RagProperties
import io.github.resilience4j.circuitbreaker.CallNotPermittedException
import io.github.resilience4j.circuitbreaker.CircuitBreaker
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig
import io.github.resilience4j.retry.Retry
import io.github.resilience4j.retry.RetryConfig
import org.springframework.stereotype.Component
import java.io.IOException
import java.net.ConnectException
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration
import java.util.function.Supplier

class DependencyHttpException(
    val status: Int,
    message: String,
) : RuntimeException(message)

data class ModelReply(
    val content: String,
    val promptTokens: Int,
    val completionTokens: Int,
)

data class EmbeddingReply(
    val vector: List<Float>,
    val promptTokens: Int,
)

data class RerankScore(
    val id: Long,
    val score: Double,
)

@Component
class DependencyCalls {
    private val client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(500)).build()
    private val breakers =
        mapOf(
            "embedding" to breaker(),
            "reranker" to breaker(),
            "llm" to breaker(),
        )

    fun json(
        dependency: String,
        request: HttpRequest,
        timeoutMs: Long,
        retryWaitMs: Long,
    ): String {
        val retry =
            Retry.of(
                "$dependency-call",
                RetryConfig
                    .custom<String>()
                    .maxAttempts(2)
                    .waitDuration(Duration.ofMillis(retryWaitMs))
                    .retryOnException(::retryable)
                    .ignoreExceptions(CallNotPermittedException::class.java)
                    .build(),
            )
        val decorated =
            Retry.decorateSupplier(
                retry,
                CircuitBreaker.decorateSupplier(
                    breakers.getValue(dependency),
                    Supplier {
                        val response = client.send(request, HttpResponse.BodyHandlers.ofString())
                        if (response.statusCode() !in 200..299) {
                            throw DependencyHttpException(response.statusCode(), "$dependency returned HTTP ${response.statusCode()}")
                        }
                        response.body()
                    },
                ),
            )
        return decorated.get()
    }

    private fun breaker(): CircuitBreaker =
        CircuitBreaker.of(
            "rag",
            CircuitBreakerConfig
                .custom()
                .slidingWindowSize(20)
                .minimumNumberOfCalls(10)
                .failureRateThreshold(50f)
                .waitDurationInOpenState(Duration.ofSeconds(30))
                .permittedNumberOfCallsInHalfOpenState(3)
                .automaticTransitionFromOpenToHalfOpenEnabled(true)
                .build(),
        )

    private fun retryable(error: Throwable): Boolean =
        error is ConnectException ||
            error is IOException &&
            error !is java.net.http.HttpTimeoutException ||
            error is DependencyHttpException &&
            error.status >= 500
}

@Component
class OllamaClient(
    private val properties: RagProperties,
    private val calls: DependencyCalls,
    private val mapper: ObjectMapper,
) {
    fun embed(text: String): EmbeddingReply {
        val body =
            mapper.writeValueAsString(
                mapOf(
                    "model" to properties.embed.model,
                    "input" to listOf(text),
                    "truncate" to true,
                    "keep_alive" to properties.ollama.keepAlive,
                ),
            )
        val node = post("/api/embed", body, properties.timeouts.embedMs, "embedding", 100)
        val values = node.path("embeddings").path(0).map(JsonNode::floatValue)
        if (values.size != 1024) throw DependencyHttpException(502, "embedding dimension must be 1024")
        return EmbeddingReply(values, node.path("prompt_eval_count").asInt(0))
    }

    fun chat(
        system: String,
        user: String,
        structured: Boolean,
        tokens: Int,
        timeoutMs: Long,
    ): ModelReply {
        val body =
            linkedMapOf<String, Any>(
                "model" to properties.llm.model,
                "messages" to listOf(mapOf("role" to "system", "content" to system), mapOf("role" to "user", "content" to user)),
                "stream" to false,
                "keep_alive" to properties.ollama.keepAlive,
                "options" to mapOf("temperature" to 0, "seed" to properties.llm.seed, "num_ctx" to 8192, "num_predict" to tokens),
            )
        if (structured) body["format"] = "json"
        val node = post("/api/chat", mapper.writeValueAsString(body), timeoutMs, "llm", 250)
        return ModelReply(
            node.path("message").path("content").asText(),
            node.path("prompt_eval_count").asInt(0),
            node.path("eval_count").asInt(0),
        )
    }

    fun tags(): JsonNode = get("/api/tags", 1000, "llm")

    private fun post(
        path: String,
        body: String,
        timeoutMs: Long,
        dependency: String,
        retryWaitMs: Long,
    ): JsonNode {
        val request =
            HttpRequest
                .newBuilder(URI.create(properties.ollama.url + path))
                .timeout(Duration.ofMillis(timeoutMs))
                .header("content-type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build()
        return mapper.readTree(calls.json(dependency, request, timeoutMs, retryWaitMs))
    }

    private fun get(
        path: String,
        timeoutMs: Long,
        dependency: String,
    ): JsonNode {
        val request =
            HttpRequest
                .newBuilder(URI.create(properties.ollama.url + path))
                .timeout(Duration.ofMillis(timeoutMs))
                .GET()
                .build()
        return mapper.readTree(calls.json(dependency, request, timeoutMs, 250))
    }
}

@Component
class RerankerClient(
    private val properties: RagProperties,
    private val calls: DependencyCalls,
    private val mapper: ObjectMapper,
) {
    fun rerank(
        question: String,
        passages: List<Pair<Long, String>>,
    ): List<RerankScore> {
        if (passages.isEmpty()) return emptyList()
        val body =
            mapper.writeValueAsString(
                mapOf(
                    "query" to question,
                    "passages" to passages.map { (id, text) -> mapOf("id" to id.toString(), "text" to text) },
                    "top_k" to passages.size,
                ),
            )
        val request =
            HttpRequest
                .newBuilder(URI.create(properties.reranker.url + "/rerank"))
                .timeout(Duration.ofMillis(properties.timeouts.rerankMs))
                .header("content-type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build()
        val response = calls.json("reranker", request, properties.timeouts.rerankMs, 200)
        return mapper.readTree(response).path("results").map {
            RerankScore(it.path("id").asLong(), it.path("score").asDouble())
        }
    }

    fun health(): JsonNode {
        val request =
            HttpRequest
                .newBuilder(URI.create(properties.reranker.url + "/healthz"))
                .timeout(Duration.ofSeconds(1))
                .GET()
                .build()
        return mapper.readTree(calls.json("reranker", request, 1000, 200))
    }
}
