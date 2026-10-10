package dev.hybridrag.service

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.context.properties.ConfigurationProperties
import org.springframework.boot.context.properties.EnableConfigurationProperties
import org.springframework.boot.runApplication

@SpringBootApplication
@EnableConfigurationProperties(RagProperties::class)
class Application

fun main(args: Array<String>) {
    runApplication<Application>(*args)
}

@ConfigurationProperties("rag")
data class RagProperties(
    var version: String = "0.1.0",
    var gitSha: String = "unknown",
    var db: DatabaseProperties = DatabaseProperties(),
    var ollama: OllamaProperties = OllamaProperties(),
    var embed: EmbedProperties = EmbedProperties(),
    var llm: LlmProperties = LlmProperties(),
    var reranker: RerankerProperties = RerankerProperties(),
    var retrieval: RetrievalProperties = RetrievalProperties(),
    var timeouts: TimeoutProperties = TimeoutProperties(),
) {
    data class DatabaseProperties(
        var url: String = "jdbc:postgresql://localhost:5433/rag",
        var reader: Credentials = Credentials("rag_reader", "rag_reader_dev", 10),
        var router: Credentials = Credentials("rag_router", "rag_router_dev", 3),
    )

    data class Credentials(
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

    data class RerankerProperties(
        var url: String = "http://localhost:8081",
    )

    data class RetrievalProperties(
        var exactVectorSearch: Boolean = false,
    )

    data class TimeoutProperties(
        var connectMs: Long = 500,
        var routeMs: Long = 5000,
        var embedMs: Long = 2000,
        var rerankMs: Long = 10000,
        var generateMs: Long = 45000,
        var sqlGenerateMs: Long = 15000,
    )
}
