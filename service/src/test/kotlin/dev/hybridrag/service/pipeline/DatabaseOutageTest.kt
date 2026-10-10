package dev.hybridrag.service.pipeline

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.hybridrag.service.testsupport.TestDatabase
import dev.hybridrag.service.testsupport.WireMockFixture
import org.junit.jupiter.api.Test
import org.springframework.boot.test.context.SpringBootTest
import org.springframework.boot.test.web.server.LocalServerPort
import org.springframework.test.annotation.DirtiesContext
import org.springframework.test.context.DynamicPropertyRegistry
import org.springframework.test.context.DynamicPropertySource
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 4.4: the database goes away (this class stops its own dedicated container) → 503 problem+json with trace_id. */
@SpringBootTest(
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = ["spring.flyway.enabled=false"],
)
@DirtiesContext
class DatabaseOutageTest {
    @LocalServerPort
    var port: Int = 0

    private val http = HttpClient.newHttpClient()

    private fun query(): HttpResponse<String> {
        val pipeline =
            """{"retrieval": {"vector": false}, "rerank": {"enabled": false}, "routing": {"enabled": false}, "generation": {"enabled": false}}"""
        val body = """{"question": "reranking benchmark", "pipeline": $pipeline}"""
        val request =
            HttpRequest
                .newBuilder(URI.create("http://localhost:$port/v1/query"))
                .header("content-type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build()
        return http.send(request, HttpResponse.BodyHandlers.ofString())
    }

    @Test
    fun `database down gives 503 problem with trace_id`() {
        assertEquals(200, query().statusCode(), "served while the database is up")
        database.container.stop()
        val started = System.nanoTime()
        val response = query()
        val elapsedMs = (System.nanoTime() - started) / 1_000_000
        assertEquals(503, response.statusCode(), response.body())
        assertTrue(
            response
                .headers()
                .firstValue("content-type")
                .get()
                .startsWith("application/problem+json"),
        )
        val json = jacksonObjectMapper().readTree(response.body())
        assertEquals(503, json["status"].asInt())
        assertTrue(json["trace_id"].asText().matches(Regex("[0-9a-f]{32}")))
        assertEquals(response.headers().firstValue("X-Trace-Id").get(), json["trace_id"].asText())
        assertTrue(elapsedMs < 5000, "fails fast (connectionTimeout 1 s), took $elapsedMs ms")
    }

    companion object {
        private val database: TestDatabase by lazy { TestDatabase().start() }

        @JvmStatic
        @DynamicPropertySource
        fun properties(registry: DynamicPropertyRegistry) {
            database.register(registry)
            registry.add("rag.ollama.url") { WireMockFixture.baseUrl }
            registry.add("rag.reranker.url") { WireMockFixture.baseUrl }
        }
    }
}
