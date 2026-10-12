package dev.hybridrag.service.routing

import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.WireMockFixture
import dev.hybridrag.service.testsupport.WireMockFixture.chatRequests
import dev.hybridrag.service.testsupport.WireMockFixture.count
import dev.hybridrag.service.testsupport.WireMockFixture.routeRequests
import dev.hybridrag.service.testsupport.WireMockFixture.sqlGenRequests
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 5.1: LLM route classification, the SQL path, and every fallback to the vector route. */
class RouteClassifierTest : IntegrationTest() {
    private val aggregate = "How many cs.IR papers did Jimmy Lin publish?"
    private val countSql =
        "select count(distinct d.id) as n from documents d join paper_categories pc on pc.document_id = d.id " +
            "join paper_authors pa on pa.document_id = d.id join authors a on a.id = pa.author_id " +
            "where pc.category = 'cs.IR' and a.name_normalized = 'jimmy lin'"

    private fun stubRetrieval() {
        WireMockFixture.stubEmbed(Fixture.vector(1, true))
        WireMockFixture.stubRerankReversing()
    }

    @Test
    fun `sql classification runs guarded SQL and answers from the rows`() {
        WireMockFixture.stubRoute("sql")
        WireMockFixture.stubSqlGen(countSql)
        WireMockFixture.stubSqlAnswer("Jimmy Lin published 2 cs.IR papers.")
        val result = query(aggregate)
        assertEquals(200, result.status, result.raw)
        val json = result.json
        assertEquals("sql", json["route"].asText())
        assertEquals("llm", json["route_source"].asText())
        assertEquals(0, json["chunks"].size())
        assertTrue(json["filters"].isNull)
        val sql = json["sql"]
        assertEquals(countSql, sql["query"].asText())
        assertEquals(listOf("n"), sql["columns"].map { it.asText() })
        assertEquals(2, sql["rows"][0][0].asInt())
        assertEquals(1, sql["row_count"].asInt())
        assertEquals(false, sql["truncated"].asBoolean())
        assertTrue(sql["error"].isNull)
        assertEquals("ok", json["answer"]["status"].asText())
        assertEquals("Jimmy Lin published 2 cs.IR papers.", json["answer"]["text"].asText())
        assertEquals(
            setOf("route", "sql.generate", "sql.execute", "generate", "total"),
            json["timings_ms"].fieldNames().asSequence().toSet(),
        )
        assertEquals(3, json["usage"]["llm_calls"].asInt())
        assertEquals(0, json["degraded"].size())
        val generationPrompt = WireMockFixture.bodies(sqlGenRequests).single()
        assertTrue("\"format\":\"json\"" in generationPrompt && "name_normalized" in generationPrompt)
    }

    @Test
    fun `garbage classification falls back to the vector route with router_unavailable`() {
        WireMockFixture.stubRouteRaw("I think this is probably SQL?")
        stubRetrieval()
        val result = query(aggregate, """{"generation": {"enabled": false}}""")
        assertEquals(200, result.status)
        assertEquals("vector", result.json["route"].asText())
        assertEquals("fallback", result.json["route_source"].asText())
        assertEquals(listOf("router_unavailable"), result.json["degraded"].map { it.asText() })
        assertTrue(result.json["sql"].isNull)
        assertTrue(result.json["chunks"].size() > 0)
        assertEquals(0, count(sqlGenRequests))
    }

    @Test
    fun `routing disabled - route_source disabled and no chat request`() {
        stubRetrieval()
        val result = query(aggregate, """{"routing": {"enabled": false}, "generation": {"enabled": false}}""")
        assertEquals("vector", result.json["route"].asText())
        assertEquals("disabled", result.json["route_source"].asText())
        assertEquals(0, count(chatRequests))
        assertTrue(
            "route" !in
                result.json["timings_ms"]
                    .fieldNames()
                    .asSequence()
                    .toList(),
        )
    }

    @Test
    fun `write statement is rejected by the guard and falls back with sql_fallback`() {
        assertFallback("delete from documents", "guard_rejected")
    }

    @Test
    fun `stacked statement is rejected and falls back`() {
        assertFallback("select 1; drop table authors", "guard_rejected")
    }

    @Test
    fun `chunks table is rejected and falls back`() {
        assertFallback("select content from chunks", "guard_rejected")
    }

    @Test
    fun `unparseable SQL generation reply falls back with generation_failed`() {
        WireMockFixture.stubRoute("sql")
        wiremock.stubFor(
            com.github.tomakehurst.wiremock.client.WireMock
                .post(
                    com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/api/chat"),
                ).withRequestBody(
                    com.github.tomakehurst.wiremock.client.WireMock
                        .matchingJsonPath(
                            "$.options.num_predict",
                            com.github.tomakehurst.wiremock.client.WireMock
                                .equalTo("256"),
                        ),
                ).willReturn(WireMockFixture.chat("not json", 1, 1)),
        )
        stubRetrieval()
        val result = query(aggregate, """{"generation": {"enabled": false}}""")
        assertTrue(result.json["sql"]["error"].asText().startsWith("generation_failed"))
        assertEquals(listOf("sql_fallback"), result.json["degraded"].map { it.asText() })
        assertEquals("fallback", result.json["route_source"].asText())
    }

    private fun assertFallback(
        generated: String,
        errorPrefix: String,
    ) {
        WireMockFixture.stubRoute("sql")
        WireMockFixture.stubSqlGen(generated)
        stubRetrieval()
        val result = query(aggregate, """{"generation": {"enabled": false}}""")
        assertEquals(200, result.status)
        val json = result.json
        assertEquals("vector", json["route"].asText())
        assertEquals("fallback", json["route_source"].asText())
        assertEquals(listOf("sql_fallback"), json["degraded"].map { it.asText() })
        assertEquals("sql_fallback", result.header("X-RAG-Degraded"))
        assertTrue(json["sql"]["error"].asText().startsWith(errorPrefix), json["sql"].toString())
        assertEquals(0, json["sql"]["rows"].size())
        assertTrue(json["chunks"].size() > 0)
        assertTrue("sql.execute" !in json["timings_ms"].fieldNames().asSequence().toList())
        assertEquals(1, count(routeRequests))
    }
}
