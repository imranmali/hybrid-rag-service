package dev.hybridrag.service.config

import org.springframework.boot.context.properties.ConfigurationProperties

/** `rag.*` configuration (design D12); every value has an env override per contract C2. */
@ConfigurationProperties("rag")
data class RagProperties(
    var version: String = "0.1.0",
    var gitSha: String = "unknown",
    var db: DatabaseProperties = DatabaseProperties(),
    var ollama: OllamaProperties = OllamaProperties(),
    var embed: EmbedProperties = EmbedProperties(),
    var llm: LlmProperties = LlmProperties(),
    var anthropic: AnthropicProperties = AnthropicProperties(),
    var reranker: RerankerProperties = RerankerProperties(),
    var retrieval: RetrievalProperties = RetrievalProperties(),
    var timeouts: TimeoutProperties = TimeoutProperties(),
    var resilience: ResilienceProperties = ResilienceProperties(),
) {
    data class DatabaseProperties(
        var url: String = "jdbc:postgresql://localhost:5433/rag",
        var reader: PoolProperties = PoolProperties("rag_reader", "rag_reader_dev", 10),
        var router: PoolProperties = PoolProperties("rag_router", "rag_router_dev", 3),
    )

    data class PoolProperties(
        var user: String = "",
        var password: String = "",
        var poolSize: Int = 1,
    )

    data class OllamaProperties(
        var url: String = "http://localhost:11434",
        var keepAlive: String = "30m",
    )

    data class EmbedProperties(
        var model: String = "bge-m3",
    )

    data class LlmProperties(
        var provider: String = "ollama",
        var model: String = "llama3.1:8b",
        var seed: Int = 42,
    )

    data class AnthropicProperties(
        var url: String = "https://api.anthropic.com",
        var apiKey: String = "",
        var model: String = "",
    )

    data class RerankerProperties(
        var url: String = "http://localhost:8081",
    )

    data class RetrievalProperties(
        var exactVectorSearch: Boolean = false,
    )

    /** Stage timeouts from contract C11; the HTTP read timeout is the hard stage timeout (design D9). */
    data class TimeoutProperties(
        var connectMs: Long = 500,
        var routeMs: Long = 5000,
        var embedMs: Long = 2000,
        var rerankMs: Long = 10000,
        var generateMs: Long = 45000,
        var sqlGenerateMs: Long = 15000,
        var infoMs: Long = 1000,
    )

    /** Breaker and retry settings from contract C11; properties so tests can shrink them. */
    data class ResilienceProperties(
        var slidingWindowSize: Int = 20,
        var minimumNumberOfCalls: Int = 10,
        var failureRateThreshold: Float = 50f,
        var waitDurationInOpenStateMs: Long = 30_000,
        var permittedNumberOfCallsInHalfOpenState: Int = 3,
        var maxAttempts: Int = 2,
        var embeddingRetryWaitMs: Long = 100,
        var rerankerRetryWaitMs: Long = 200,
        var llmRetryWaitMs: Long = 250,
    )
}
