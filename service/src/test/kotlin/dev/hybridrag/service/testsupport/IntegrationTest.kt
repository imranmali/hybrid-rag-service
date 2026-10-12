package dev.hybridrag.service.testsupport

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.hybridrag.service.resilience.DependencyGuards
import io.opentelemetry.api.trace.Tracer
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter
import io.opentelemetry.sdk.trace.SdkTracerProvider
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor
import org.junit.jupiter.api.BeforeEach
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.boot.test.context.TestConfiguration
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Import
import org.springframework.context.annotation.Primary
import org.springframework.test.context.DynamicPropertyRegistry
import org.springframework.test.context.DynamicPropertySource
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration

/** Replaces the global (no-op without the agent) tracer with an SDK tracer exporting to memory (design D10). */
@TestConfiguration(proxyBeanMethods = false)
class TestTracing {
    @Bean
    @Primary
    fun testTracer(): Tracer = provider.get("dev.hybridrag.service")

    companion object {
        val exporter: InMemorySpanExporter = InMemorySpanExporter.create()
        val provider: SdkTracerProvider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()
    }
}

data class HttpResult(
    val status: Int,
    val headers: Map<String, List<String>>,
    val raw: String,
) {
    val json: JsonNode by lazy { jacksonObjectMapper().readTree(raw) }

    fun header(name: String): String? =
        headers.entries
            .firstOrNull { it.key.equals(name, ignoreCase = true) }
            ?.value
            ?.firstOrNull()
}

/**
 * Base for integration tests: the app on a random port against the shared seeded Testcontainers database and the
 * shared WireMock server, with shrunken timeouts and breaker windows (design D13).
 */
@SpringBootTest(
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = [
        "spring.flyway.enabled=false",
        "rag.timeouts.embed-ms=1000",
        "rag.timeouts.route-ms=1000",
        "rag.timeouts.sql-generate-ms=1000",
        "rag.timeouts.rerank-ms=500",
        "rag.timeouts.generate-ms=1000",
        "rag.timeouts.info-ms=1000",
        "rag.resilience.sliding-window-size=4",
        "rag.resilience.minimum-number-of-calls=4",
        "rag.resilience.embedding-retry-wait-ms=10",
        "rag.resilience.reranker-retry-wait-ms=10",
        "rag.resilience.llm-retry-wait-ms=10",
    ],
)
@Import(TestTracing::class)
abstract class IntegrationTest {
    @LocalServerPort
    var port: Int = 0

    @Autowired
    lateinit var guards: DependencyGuards

    protected val wiremock get() = WireMockFixture.server
    private val http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()

    @BeforeEach
    fun resetDependencies() {
        WireMockFixture.server.resetAll()
        guards.resetAll()
        TestTracing.exporter.reset()
    }

    fun post(
        path: String,
        body: String,
    ): HttpResult {
        val request =
            HttpRequest
                .newBuilder(URI.create("http://localhost:$port$path"))
                .timeout(Duration.ofSeconds(60))
                .header("content-type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build()
        val response = http.send(request, HttpResponse.BodyHandlers.ofString())
        return HttpResult(response.statusCode(), response.headers().map(), response.body())
    }

    fun get(path: String): HttpResult {
        val request = HttpRequest.newBuilder(URI.create("http://localhost:$port$path")).GET().build()
        val response = http.send(request, HttpResponse.BodyHandlers.ofString())
        return HttpResult(response.statusCode(), response.headers().map(), response.body())
    }

    /** `POST /v1/query` with the given question and raw `pipeline` JSON object. */
    fun query(
        question: String,
        pipeline: String = "{}",
    ): HttpResult = post("/v1/query", """{"question": ${jacksonObjectMapper().writeValueAsString(question)}, "pipeline": $pipeline}""")

    companion object {
        /** Everything model-related off: a pure retrieval request. */
        const val RETRIEVAL_ONLY = """{"rerank": {"enabled": false}, "routing": {"enabled": false}, "generation": {"enabled": false}}"""

        @JvmStatic
        @DynamicPropertySource
        fun properties(registry: DynamicPropertyRegistry) {
            TestDatabase.shared.register(registry)
            registry.add("rag.ollama.url") { WireMockFixture.baseUrl }
            registry.add("rag.reranker.url") { WireMockFixture.baseUrl }
        }
    }
}
