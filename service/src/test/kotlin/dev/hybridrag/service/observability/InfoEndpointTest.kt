package dev.hybridrag.service.observability

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.TestDatabase
import dev.hybridrag.service.testsupport.WireMockFixture
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.extension.ExtendWith
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.system.CapturedOutput
import org.springframework.boot.test.system.OutputCaptureExtension
import java.util.UUID
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Task 7.2: `GET /v1/info` (contract C6) and the embedding digest check. */
@ExtendWith(OutputCaptureExtension::class)
class InfoEndpointTest : IntegrationTest() {
    @Autowired
    lateinit var digestCheck: DigestCheck

    private fun setChunkCount(value: String) {
        TestDatabase.shared.superuser().use {
            it.prepareStatement("update index_metadata set value = ?, updated_at = now() where key = 'chunk_count'").use { s ->
                s.setString(1, value)
                s.executeUpdate()
            }
        }
    }

    @Test
    fun `info reports service, models, live index metadata, digest match and defaults`() {
        WireMockFixture.stubTags(Fixture.INDEX_DIGEST, llmDigest = "3".repeat(64))
        WireMockFixture.stubRerankerHealth()
        val json = get("/v1/info").json
        assertEquals("0.1.0", json["service"]["version"].asText())
        assertTrue(json["service"].has("git_sha"))
        assertEquals("bge-m3", json["models"]["embedding"]["name"].asText())
        assertEquals(Fixture.INDEX_DIGEST, json["models"]["embedding"]["digest"].asText())
        assertEquals("ollama", json["models"]["llm"]["provider"].asText())
        assertEquals("llama3.1:8b", json["models"]["llm"]["name"].asText())
        assertEquals("3".repeat(64), json["models"]["llm"]["digest"].asText())
        assertEquals(WireMockFixture.baseUrl, json["models"]["reranker"]["url"].asText())
        assertEquals(true, json["models"]["reranker"]["reachable"].asBoolean())
        assertEquals("BAAI/bge-reranker-v2-m3", json["models"]["reranker"]["model"].asText())
        assertEquals("abc123", json["models"]["reranker"]["revision"].asText())
        assertEquals(Fixture.CHUNKS.toString(), json["index"]["chunk_count"].asText())
        assertEquals(true, json["embedding_digest_matches_index"].asBoolean())
        assertEquals(false, json["exact_vector_search"].asBoolean())
        assertEquals(60, json["defaults"]["retrieval"]["fusion"]["k"].asInt())
        assertTrue(json["defaults"]["filters"]["explicit"].isNull)
    }

    @Test
    fun `index_metadata changes are reflected without restart`() {
        try {
            setChunkCount("4242")
            assertEquals("4242", get("/v1/info").json["index"]["chunk_count"].asText())
        } finally {
            setChunkCount(Fixture.CHUNKS.toString())
        }
        assertEquals(Fixture.CHUNKS.toString(), get("/v1/info").json["index"]["chunk_count"].asText())
    }

    @Test
    fun `mismatching digest is reported false and logged at ERROR`(output: CapturedOutput) {
        val live =
            UUID
                .randomUUID()
                .toString()
                .replace("-", "")
                .repeat(2)
        WireMockFixture.stubTags(live)
        digestCheck.onStartup() // the ApplicationReadyEvent check, re-run against the stubbed live digest
        val line =
            output.out
                .lines()
                .filter { "embedding_digest_mismatch" in it }
                .map { jacksonObjectMapper().readTree(it) }
                .single { it["live_digest"].asText() == live }
        assertEquals("ERROR", line["level"].asText())
        assertEquals(Fixture.INDEX_DIGEST, line["index_digest"].asText())
        assertEquals(false, get("/v1/info").json["embedding_digest_matches_index"].asBoolean())
    }

    @Test
    fun `unreachable models give null digests and an unreachable reranker without failing`() {
        val json = get("/v1/info").json
        assertEquals(200, get("/v1/info").status)
        assertTrue(json["models"]["embedding"]["digest"].isNull)
        assertEquals(false, json["models"]["reranker"]["reachable"].asBoolean())
        assertNull(json["models"]["reranker"]["model"].textValue())
        assertEquals(false, json["embedding_digest_matches_index"].asBoolean())
    }
}
