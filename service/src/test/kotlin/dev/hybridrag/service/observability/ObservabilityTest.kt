package dev.hybridrag.service.observability

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.post
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.TestTracing
import dev.hybridrag.service.testsupport.WireMockFixture
import io.opentelemetry.api.trace.SpanKind
import io.opentelemetry.api.trace.StatusCode
import io.opentelemetry.sdk.trace.data.SpanData
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.extension.ExtendWith
import org.springframework.boot.test.system.CapturedOutput
import org.springframework.boot.test.system.OutputCaptureExtension
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 7.1: stage spans under one parent, error status, and trace ids in JSON log lines (design D10). */
@ExtendWith(OutputCaptureExtension::class)
class ObservabilityTest : IntegrationTest() {
    private val mapper = jacksonObjectMapper()

    private fun stubAll() {
        WireMockFixture.stubEmbed(Fixture.vector(Fixture.chunkId(1, 2), true))
        WireMockFixture.stubRoute("vector")
        WireMockFixture.stubRerankReversing()
        WireMockFixture.stubAnswer("Answer [1].")
    }

    /** The server span ends in the filter after the response is written, so wait for it. */
    private fun serverSpan(traceId: String): SpanData {
        val deadline = System.currentTimeMillis() + 5000
        while (System.currentTimeMillis() < deadline) {
            TestTracing.exporter.finishedSpanItems
                .firstOrNull { it.traceId == traceId && it.kind == SpanKind.SERVER }
                ?.let { return it }
            Thread.sleep(20)
        }
        error("no server span for trace $traceId")
    }

    private fun logLines(output: CapturedOutput): List<JsonNode> =
        output.out
            .lines()
            .filter { it.startsWith("{") }
            .mapNotNull { runCatching { mapper.readTree(it) }.getOrNull() }

    @Test
    fun `full vector-routed pipeline emits exactly the eight stage spans under one parent`() {
        stubAll()
        val result = query("How is reranking evaluated?")
        assertEquals(200, result.status)
        val traceId = result.json["trace_id"].asText()
        val server = serverSpan(traceId)
        assertEquals("POST /v1/query", server.name)
        val children = TestTracing.exporter.finishedSpanItems.filter { it.traceId == traceId && it.parentSpanId == server.spanId }
        assertEquals(
            listOf("route", "filter.estimate", "embed", "retrieve.vector", "retrieve.bm25", "fuse", "rerank", "generate").sorted(),
            children.map { it.name }.sorted(),
        )
        assertTrue(
            TestTracing.exporter.finishedSpanItems
                .filter { it.traceId == traceId }
                .size == 9,
        )
        val byName = children.associateBy { it.name }
        assertEquals(
            "vector",
            byName
                .getValue("route")
                .attributes
                .asMap()
                .entries
                .first { it.key.key == "rag.route" }
                .value,
        )
        assertEquals(
            "none",
            byName
                .getValue("filter.estimate")
                .attributes
                .asMap()
                .entries
                .first { it.key.key == "rag.filter.strategy" }
                .value,
        )
        assertEquals(
            20L,
            byName
                .getValue("rerank")
                .attributes
                .asMap()
                .entries
                .first { it.key.key == "rag.rerank.pairs" }
                .value,
        )
        assertEquals(
            900L,
            byName
                .getValue("generate")
                .attributes
                .asMap()
                .entries
                .first { it.key.key == "gen_ai.usage.input_tokens" }
                .value,
        )
        assertEquals(
            "llama3.1:8b",
            byName
                .getValue("generate")
                .attributes
                .asMap()
                .entries
                .first { it.key.key == "gen_ai.request.model" }
                .value,
        )
        assertTrue(children.all { it.status.statusCode != StatusCode.ERROR })
    }

    @Test
    fun `failed rerank span has ERROR status and the request still returns 200`() {
        stubAll()
        WireMockFixture.stubRerankStatus(500)
        val result = query("How is reranking evaluated?")
        assertEquals(200, result.status)
        val traceId = result.json["trace_id"].asText()
        serverSpan(traceId)
        val rerank = TestTracing.exporter.finishedSpanItems.single { it.traceId == traceId && it.name == "rerank" }
        assertEquals(StatusCode.ERROR, rerank.status.statusCode)
        assertTrue(rerank.events.any { it.name == "exception" })
    }

    @Test
    fun `one query_completed JSON line whose trace_id equals the response's`(output: CapturedOutput) {
        stubAll()
        val result = query("How is reranking evaluated?")
        val traceId = result.json["trace_id"].asText()
        val completed = logLines(output).filter { it["message"]?.asText() == "query_completed" && it["trace_id"]?.asText() == traceId }
        assertEquals(1, completed.size, output.out)
        val line = completed.single()
        assertEquals(result.json["request_id"].asText(), line["request_id"].asText())
        assertEquals("vector", line["route"].asText())
        assertEquals("llm", line["route_source"].asText())
        assertEquals("ok", line["answer_status"].asText())
        assertEquals(50, line["chunks"].asInt())
        assertTrue(line["timings_ms"].has("total"))
        assertTrue(line["degraded"].isArray)
        assertTrue(line.has("filter_strategy"))
        assertEquals("INFO", line["level"].asText())
    }

    @Test
    fun `reranker failure logs a dependency_degraded warning with the request's trace id`(output: CapturedOutput) {
        stubAll()
        WireMockFixture.stubRerankStatus(500)
        val traceId = query("How is reranking evaluated?").json["trace_id"].asText()
        val degraded = logLines(output).filter { it["message"]?.asText() == "dependency_degraded" && it["trace_id"]?.asText() == traceId }
        assertEquals(1, degraded.size, output.out)
        assertEquals("reranker", degraded.single()["dependency"].asText())
        assertEquals("WARN", degraded.single()["level"].asText())
        assertTrue(degraded.single().has("breaker_state"))
    }

    @Test
    fun `log lines written on worker threads carry the trace id`(output: CapturedOutput) {
        wiremock.stubFor(post(urlEqualTo("/api/embed")).willReturn(aResponse().withStatus(500)))
        val result = query("reranking benchmark", RETRIEVAL_ONLY)
        val traceId = result.json["trace_id"].asText()
        val line = logLines(output).single { it["message"]?.asText() == "dependency_degraded" && it["dependency"]?.asText() == "embedding" }
        assertEquals(traceId, line["trace_id"].asText())
        assertTrue(
            line["thread_name"].asText() !=
                logLines(
                    output,
                ).single { it["message"]?.asText() == "query_completed" }["thread_name"].asText(),
        )
    }

    @Test
    fun `error responses carry the trace id in body and header`() {
        val result = post("/v1/query", """{"question": ""}""")
        assertEquals(400, result.status)
        assertEquals(result.header("X-Trace-Id"), result.json["trace_id"].asText())
    }
}
