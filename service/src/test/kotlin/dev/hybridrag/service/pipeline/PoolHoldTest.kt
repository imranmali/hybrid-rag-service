package dev.hybridrag.service.pipeline

import com.zaxxer.hikari.HikariDataSource
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.WireMockFixture
import dev.hybridrag.service.testsupport.WireMockFixture.answerRequests
import dev.hybridrag.service.testsupport.WireMockFixture.count
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.test.context.TestPropertySource
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import kotlin.test.assertEquals

/** Task 4.4: no reader connection is held while 12 concurrent full-pipeline queries wait on a slow LLM. */
@TestPropertySource(properties = ["rag.timeouts.generate-ms=20000"])
class PoolHoldTest : IntegrationTest() {
    @Autowired
    @Qualifier("readerDataSource")
    lateinit var reader: HikariDataSource

    @Test
    fun `reader pool has no active connections while 12 queries wait on generation`() {
        WireMockFixture.stubEmbed(Fixture.vector(Fixture.chunkId(1, 1), true))
        WireMockFixture.stubRoute("vector")
        WireMockFixture.stubRerankReversing()
        WireMockFixture.stubAnswer("slow [1]", WireMockFixture.chat("slow [1]", 10, 2).withFixedDelay(4000))

        val client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()
        val pending =
            (1..12).map { i ->
                val body = """{"question": "reranking question $i"}"""
                client.sendAsync(
                    HttpRequest
                        .newBuilder(URI.create("http://localhost:$port/v1/query"))
                        .header("content-type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body))
                        .build(),
                    HttpResponse.BodyHandlers.ofString(),
                )
            }

        val deadline = System.currentTimeMillis() + 10_000
        while (count(answerRequests) < 12 && System.currentTimeMillis() < deadline) Thread.sleep(20)
        assertEquals(12, count(answerRequests), "all 12 queries are waiting on generation")
        val pool = reader.hikariPoolMXBean
        val samples = (1..10).map { pool.activeConnections.also { Thread.sleep(50) } }
        assertEquals(List(10) { 0 }, samples, "reader activeConnections while 12 requests wait on the LLM")
        assertEquals(12, pending.map { it.get() }.count { it.statusCode() == 200 && "\"status\":\"ok\"" in it.body() })
    }
}
