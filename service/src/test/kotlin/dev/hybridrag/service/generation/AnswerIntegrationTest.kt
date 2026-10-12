package dev.hybridrag.service.generation

import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.WireMockFixture
import dev.hybridrag.service.testsupport.WireMockFixture.answerRequests
import dev.hybridrag.service.testsupport.WireMockFixture.chatRequests
import dev.hybridrag.service.testsupport.WireMockFixture.count
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 6.1 (WireMock): generation switches, SQL route answers and the contextual-independent prompt. */
class AnswerIntegrationTest : IntegrationTest() {
    @Test
    fun `generation disabled - no chat request and answer disabled`() {
        WireMockFixture.stubEmbed(Fixture.vector(1, true))
        val result =
            query("reranking", """{"rerank": {"enabled": false}, "routing": {"enabled": false}, "generation": {"enabled": false}}""")
        val answer = result.json["answer"]
        assertEquals("disabled", answer["status"].asText())
        assertTrue(answer["http_status"].isNull && answer["text"].isNull && answer["citations"].isNull)
        assertEquals(false, answer["abstained"].asBoolean())
        assertEquals(0, count(chatRequests))
    }

    @Test
    fun `SQL route with generation disabled returns rows and answer disabled`() {
        WireMockFixture.stubRoute("sql")
        WireMockFixture.stubSqlGen("select title from documents order by title")
        val result = query("List all paper titles", """{"generation": {"enabled": false}}""")
        assertEquals("sql", result.json["route"].asText())
        assertEquals(Fixture.DOCUMENTS, result.json["sql"]["rows"].size())
        assertEquals("disabled", result.json["answer"]["status"].asText())
        assertTrue(result.json["answer"]["http_status"].isNull)
        assertEquals(0, count(answerRequests))
    }

    @Test
    fun `abstained answer is ok and flagged`() {
        WireMockFixture.stubEmbed(Fixture.vector(1, true))
        WireMockFixture.stubAnswer("I cannot answer this from the indexed papers.")
        val result = query("What is the capital of France?", """{"rerank": {"enabled": false}, "routing": {"enabled": false}}""")
        val answer = result.json["answer"]
        assertEquals("ok", answer["status"].asText())
        assertEquals(true, answer["abstained"].asBoolean())
        assertEquals(0, answer["citations"].size())
    }

    @Test
    fun `context_k bounds the passages and citations`() {
        WireMockFixture.stubEmbed(Fixture.vector(1, true))
        WireMockFixture.stubAnswer("A [1] B [3] C [4]")
        val result = query("reranking", """{"rerank": {"enabled": false}, "routing": {"enabled": false}, "generation": {"context_k": 3}}""")
        val chunks = result.json["chunks"]
        assertEquals(
            listOf(chunks[0]["chunk_id"].asLong(), chunks[2]["chunk_id"].asLong()),
            result.json["answer"]["citations"].map { it.asLong() },
        )
        val prompt = WireMockFixture.bodies(answerRequests).single()
        assertTrue("[3] " in prompt && "[4] " !in prompt, prompt)
    }

    @Test
    fun `generation prompt is identical for contextual true and false`() {
        WireMockFixture.stubAnswer("Benchmark collection c3x7 [1].")
        // A token that occurs only in the content of chunk (3, 7) makes both variants retrieve exactly that chunk.
        val pipeline = """{"retrieval": {"vector": false}, "rerank": {"enabled": false}, "routing": {"enabled": false}, "contextual": %s}"""
        val contextual = query("c3x7", pipeline.format("true"))
        val plain = query("c3x7", pipeline.format("false"))
        assertEquals(listOf(Fixture.chunkId(3, 7)), contextual.json["chunks"].map { it["chunk_id"].asLong() })
        assertEquals(listOf(Fixture.chunkId(3, 7)), plain.json["chunks"].map { it["chunk_id"].asLong() })
        val prompts = WireMockFixture.bodies(answerRequests)
        assertEquals(2, prompts.size)
        assertEquals(prompts[0], prompts[1])
        assertTrue("Paper: " !in prompts[0], "the context header is never part of the generation prompt")
    }
}
