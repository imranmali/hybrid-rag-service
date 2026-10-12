package dev.hybridrag.service.pipeline

import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.post
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import com.github.tomakehurst.wiremock.http.Fault
import dev.hybridrag.service.api.PipelineDefaults
import dev.hybridrag.service.retrieval.FilterPlannerTest.Companion.MODELS_OFF
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.WireMockFixture
import dev.hybridrag.service.testsupport.WireMockFixture.answerRequests
import dev.hybridrag.service.testsupport.WireMockFixture.count
import dev.hybridrag.service.testsupport.WireMockFixture.embedRequests
import dev.hybridrag.service.testsupport.WireMockFixture.rerankRequests
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.http.converter.json.Jackson2ObjectMapperBuilder
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Task 4.3: the wired pipeline end to end on the seeded database, including every degraded scenario. */
class QueryPipelineTest : IntegrationTest() {
    @Autowired
    lateinit var mapperBuilder: Jackson2ObjectMapperBuilder

    private val target = Fixture.chunkId(0, 4)
    private val question = "How is reranking evaluated?"
    private val mapper =
        com.fasterxml.jackson.module.kotlin
            .jacksonObjectMapper()

    private fun stubHappyPath() {
        WireMockFixture.stubEmbed(Fixture.vector(target, true))
        WireMockFixture.stubRoute("vector")
        WireMockFixture.stubRerankReversing()
        WireMockFixture.stubAnswer("Rerankers are evaluated on benchmark collections [1][2].")
    }

    private fun fusedOrder(json: com.fasterxml.jackson.databind.JsonNode): List<Long> =
        json["chunks"]
            .sortedWith(
                compareByDescending<com.fasterxml.jackson.databind.JsonNode> {
                    it["scores"]["fused"].asDouble()
                }.thenBy { it["chunk_id"].asLong() },
            ).map { it["chunk_id"].asLong() }

    @Test
    fun `full pipeline - all stages, rerank merge, citations and usage`() {
        stubHappyPath()
        val result = query(question)
        assertEquals(200, result.status, result.raw)
        val json = result.json
        assertEquals("vector", json["route"].asText())
        assertEquals("llm", json["route_source"].asText())
        assertEquals(0, json["degraded"].size())
        assertNull(result.header("X-RAG-Degraded"))
        assertEquals(json["trace_id"].asText(), result.header("X-Trace-Id"))
        assertTrue(json["trace_id"].asText().matches(Regex("[0-9a-f]{32}")))
        assertEquals(
            setOf("route", "filter.estimate", "embed", "retrieve.vector", "retrieve.bm25", "fuse", "rerank", "generate", "total"),
            json["timings_ms"].fieldNames().asSequence().toSet(),
        )
        val chunks = json["chunks"]
        assertEquals(50, chunks.size())
        assertEquals((1..50).toList(), chunks.map { it["rank"].asInt() })
        // The stub reranker reverses its input: ranks 1-20 are fused ranks 20..1, ranks 21-50 keep fused order.
        val head = chunks.take(20)
        head.zipWithNext().forEach { (a, b) -> assertTrue(a["scores"]["rerank"].asDouble() > b["scores"]["rerank"].asDouble()) }
        val tail = chunks.drop(20)
        tail.forEach { assertTrue(it["scores"]["rerank"].isNull) }
        tail.zipWithNext().forEach { (a, b) -> assertTrue(a["scores"]["fused"].asDouble() >= b["scores"]["fused"].asDouble()) }
        assertEquals(head.minOf { it["scores"]["fused"].asDouble() }, head.first()["scores"]["fused"].asDouble())
        val answer = json["answer"]
        assertEquals("ok", answer["status"].asText())
        assertEquals(200, answer["http_status"].asInt())
        assertEquals(listOf(chunks[0]["chunk_id"].asLong(), chunks[1]["chunk_id"].asLong()), answer["citations"].map { it.asLong() })
        assertEquals(false, answer["abstained"].asBoolean())
        val usage = json["usage"]
        assertEquals(7, usage["embed_prompt_tokens"].asInt())
        assertEquals(20, usage["rerank_pairs"].asInt())
        assertEquals(2, usage["llm_calls"].asInt())
        assertEquals(950, usage["llm_prompt_tokens"].asInt())
        assertEquals(63, usage["llm_completion_tokens"].asInt())
        // contextual = true sends header + content to the reranker
        val rerankBody = WireMockFixture.bodies(rerankRequests).single()
        assertTrue("Paper: " in rerankBody && "\\n\\n" in rerankBody, rerankBody)
    }

    @Test
    fun `minimal request echoes the fully resolved default pipeline`() {
        stubHappyPath()
        val echoed = query(question).json["pipeline"]
        val expected =
            mapperBuilder
                .build<com.fasterxml.jackson.databind.ObjectMapper>()
                .valueToTree<com.fasterxml.jackson.databind.JsonNode>(
                    PipelineDefaults.PIPELINE,
                )
        assertEquals(expected, echoed)
        assertTrue(echoed["filters"].has("explicit") && echoed["filters"]["explicit"].isNull)
    }

    @Test
    fun `explicit filters are echoed in the contract shape`() {
        stubHappyPath()
        val explicit =
            """{"categories": ["cs.IR"], "authors": ["Jimmy Lin"], "published_from": "2024-01-01", "published_to": "2024-12-31"}"""
        val result =
            query(question, """{"filters": {"explicit": $explicit}, $MODELS_OFF}""")
        assertEquals(mapper.readTree(explicit), result.json["pipeline"]["filters"]["explicit"])
        assertEquals(
            setOf("categories", "authors", "published_from", "published_to"),
            result.json["filters"]["applied"]
                .fieldNames()
                .asSequence()
                .toSet(),
        )
        assertEquals("jimmy lin", result.json["filters"]["applied"]["authors"][0].asText())
    }

    @Test
    fun `contextual false sends content only to the reranker`() {
        stubHappyPath()
        WireMockFixture.stubEmbed(Fixture.vector(target, false))
        query(question, """{"contextual": false, "routing": {"enabled": false}, "generation": {"enabled": false}}""")
        val rerankBody = WireMockFixture.bodies(rerankRequests).single()
        assertTrue("Paper: " !in rerankBody, rerankBody)
    }

    @Test
    fun `reranker 500 - fused order and rerank_unavailable`() {
        stubHappyPath()
        WireMockFixture.stubRerankStatus(500)
        val result = query(question)
        assertEquals(200, result.status)
        assertEquals(listOf("rerank_unavailable"), result.json["degraded"].map { it.asText() })
        assertEquals("rerank_unavailable", result.header("X-RAG-Degraded"))
        assertEquals(fusedOrder(result.json), result.json["chunks"].map { it["chunk_id"].asLong() })
        assertTrue(result.json["chunks"].all { it["scores"]["rerank"].isNull })
        assertTrue(result.json["timings_ms"].has("rerank"))
        assertEquals(2, count(rerankRequests), "5xx is retried once")
    }

    @Test
    fun `reranker slower than its timeout - request returns before the delay`() {
        stubHappyPath()
        wiremock.stubFor(post(urlEqualTo("/rerank")).willReturn(aResponse().withFixedDelay(5000).withStatus(200)))
        val started = System.nanoTime()
        val result = query(question, """{"routing": {"enabled": false}, "generation": {"enabled": false}}""")
        val elapsedMs = (System.nanoTime() - started) / 1_000_000
        assertEquals(200, result.status)
        assertEquals(listOf("rerank_unavailable"), result.json["degraded"].map { it.asText() })
        assertTrue(elapsedMs < 4000, "took $elapsedMs ms")
        assertEquals(fusedOrder(result.json), result.json["chunks"].map { it["chunk_id"].asLong() })
        assertEquals(1, count(rerankRequests), "read timeouts are not retried")
    }

    @Test
    fun `reranker 503 then 200 - served with rerank scores after exactly two requests`() {
        stubHappyPath()
        WireMockFixture.stubRerank503Then200()
        val result = query(question)
        assertEquals(200, result.status)
        assertEquals(0, result.json["degraded"].size())
        assertTrue(result.json["chunks"][0]["scores"]["rerank"].isNumber)
        assertEquals(2, count(rerankRequests))
    }

    @Test
    fun `open reranker breaker short-circuits - request count stops increasing`() {
        stubHappyPath()
        WireMockFixture.stubRerankStatus(500)
        val retrievalOnly = """{"routing": {"enabled": false}, "generation": {"enabled": false}}"""
        repeat(2) { query(question, retrievalOnly) } // 2 requests x 2 attempts = 4 failures in a window of 4
        assertEquals(4, count(rerankRequests))
        val result = query(question, retrievalOnly)
        assertEquals(200, result.status)
        assertEquals(listOf("rerank_unavailable"), result.json["degraded"].map { it.asText() })
        assertEquals(4, count(rerankRequests))
        assertEquals(200, get("/actuator/health").status, "open breakers do not fail health")
        assertEquals("UP", get("/actuator/health").json["status"].asText())
    }

    @Test
    fun `LLM refused - 200 with chunks, answer 503, llm_unavailable and router_unavailable`() {
        WireMockFixture.stubEmbed(Fixture.vector(target, true))
        WireMockFixture.stubRerankReversing()
        wiremock.stubFor(post(urlEqualTo("/api/chat")).willReturn(aResponse().withFault(Fault.CONNECTION_RESET_BY_PEER)))
        val result = query(question)
        assertEquals(200, result.status)
        assertTrue(result.json["chunks"].size() > 0)
        assertEquals("fallback", result.json["route_source"].asText())
        assertEquals("unavailable", result.json["answer"]["status"].asText())
        assertEquals(503, result.json["answer"]["http_status"].asInt())
        assertTrue(result.json["answer"]["text"].isNull && result.json["answer"]["citations"].isNull)
        assertEquals(setOf("llm_unavailable", "router_unavailable"), result.json["degraded"].map { it.asText() }.toSet())
        assertEquals(0, result.json["usage"]["llm_calls"].asInt())
    }

    @Test
    fun `generation timeout - exactly one generation request and answer unavailable`() {
        stubHappyPath()
        WireMockFixture.stubAnswer("late", WireMockFixture.chat("late", 1, 1).withFixedDelay(4000))
        val result = query(question, """{"routing": {"enabled": false}}""")
        assertEquals(200, result.status)
        assertEquals("unavailable", result.json["answer"]["status"].asText())
        assertEquals(503, result.json["answer"]["http_status"].asInt())
        assertEquals(listOf("llm_unavailable"), result.json["degraded"].map { it.asText() })
        assertTrue(result.json["timings_ms"].has("generate"))
        assertEquals(1, count(answerRequests))
    }

    @Test
    fun `embedding down with bm25 - lexical only with embedding_unavailable`() {
        wiremock.stubFor(post(urlEqualTo("/api/embed")).willReturn(aResponse().withStatus(500)))
        val result = query("reranking benchmark", RETRIEVAL_ONLY)
        assertEquals(200, result.status)
        assertEquals(listOf("embedding_unavailable"), result.json["degraded"].map { it.asText() })
        val chunks = result.json["chunks"]
        assertTrue(chunks.size() > 0)
        assertTrue(chunks.all { it["scores"]["vector"].isNull && it["scores"]["vector_rank"].isNull && it["scores"]["bm25"].isNumber })
        val timings =
            result.json["timings_ms"]
                .fieldNames()
                .asSequence()
                .toSet()
        assertTrue("embed" in timings && "retrieve.vector" !in timings && "fuse" !in timings, timings.toString())
        assertEquals(2, count(embedRequests))
    }

    @Test
    fun `embedding down without bm25 - 503 problem`() {
        wiremock.stubFor(post(urlEqualTo("/api/embed")).willReturn(aResponse().withStatus(500)))
        val result =
            query(
                "reranking benchmark",
                """{"retrieval": {"bm25": false}, "rerank": {"enabled": false}, "routing": {"enabled": false}, "generation": {"enabled": false}}""",
            )
        assertEquals(503, result.status)
        assertTrue(result.header("content-type")!!.startsWith("application/problem+json"))
        assertEquals(result.header("X-Trace-Id"), result.json["trace_id"].asText())
    }

    @Test
    fun `skipped stages are absent from timings and llm_calls is 0`() {
        WireMockFixture.stubEmbed(Fixture.vector(target, true))
        val result = query(question, RETRIEVAL_ONLY)
        val timings =
            result.json["timings_ms"]
                .fieldNames()
                .asSequence()
                .toSet()
        assertEquals(setOf("filter.estimate", "embed", "retrieve.vector", "retrieve.bm25", "fuse", "total"), timings)
        assertEquals(0, result.json["usage"]["llm_calls"].asInt())
        assertEquals(0, result.json["usage"]["rerank_pairs"].asInt())
        assertEquals("disabled", result.json["route_source"].asText())
        assertEquals("disabled", result.json["answer"]["status"].asText())
        assertTrue(result.json["answer"]["http_status"].isNull)
        assertEquals(0, WireMockFixture.count(WireMockFixture.chatRequests))
    }

    @Test
    fun `vector-only request - vector scores set, everything else null`() {
        WireMockFixture.stubEmbed(Fixture.vector(target, true))
        val result =
            query(
                question,
                """{"retrieval": {"bm25": false, "return_k": 10}, "rerank": {"enabled": false, "candidates": 5}, "routing": {"enabled": false}, "generation": {"enabled": false}}""",
            )
        val chunks = result.json["chunks"]
        assertEquals(10, chunks.size())
        assertEquals(target, chunks[0]["chunk_id"].asLong())
        chunks.forEach {
            val s = it["scores"]
            assertTrue(s["vector"].isNumber && s["vector_rank"].isNumber)
            assertTrue(s["bm25"].isNull && s["bm25_rank"].isNull && s["fused"].isNull && s["rerank"].isNull)
        }
        val first = chunks[0]
        val doc = Fixture.docOf(target)
        assertEquals(doc.externalId, first["external_id"].asText())
        assertEquals(4, first["ordinal"].asInt())
        assertEquals(doc.title, first["title"].asText())
        assertEquals(doc.publishedOn.toString(), first["published_on"].asText())
        assertEquals(doc.category, first["primary_category"].asText())
        assertTrue(first["context_header"].asText().startsWith("Paper: "))
    }
}
