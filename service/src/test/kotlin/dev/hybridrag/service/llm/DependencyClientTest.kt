package dev.hybridrag.service.llm

import com.github.tomakehurst.wiremock.client.WireMock.absent
import com.github.tomakehurst.wiremock.client.WireMock.equalTo
import com.github.tomakehurst.wiremock.client.WireMock.equalToJson
import com.github.tomakehurst.wiremock.client.WireMock.matchingJsonPath
import com.github.tomakehurst.wiremock.client.WireMock.post
import com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import dev.hybridrag.service.Application
import dev.hybridrag.service.config.HttpClients
import dev.hybridrag.service.config.RagProperties
import dev.hybridrag.service.rerank.RerankPassage
import dev.hybridrag.service.rerank.RerankerClient
import dev.hybridrag.service.resilience.DependencyGuards
import dev.hybridrag.service.resilience.MalformedResponseException
import dev.hybridrag.service.testsupport.WireMockFixture
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows
import org.springframework.boot.builder.SpringApplicationBuilder
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 4.1: exact request bodies of contracts C3 / C5 and provider selection. */
class DependencyClientTest {
    private val server = WireMockFixture.server
    private val properties =
        RagProperties(
            ollama = RagProperties.OllamaProperties(url = WireMockFixture.baseUrl),
            reranker = RagProperties.RerankerProperties(url = WireMockFixture.baseUrl),
            anthropic = RagProperties.AnthropicProperties(url = WireMockFixture.baseUrl, apiKey = "test-key", model = "claude-test"),
        )
    private val http = HttpClients(properties)
    private val guards = DependencyGuards(properties)
    private val ollama = OllamaLlmClient(properties, http, guards)

    @BeforeEach
    fun reset() = server.resetAll()

    @Test
    fun `embedding request has exactly one input and the contract fields`() {
        WireMockFixture.stubEmbed(FloatArray(1024) { if (it == 0) 1f else 0f })
        val embedding = EmbeddingClient(properties, http, guards).embed("What is ColBERT?")
        assertEquals(1024, embedding.vector.size)
        assertEquals(7, embedding.promptTokens)
        server.verify(
            postRequestedFor(urlEqualTo("/api/embed"))
                .withRequestBody(
                    equalToJson("""{"model": "bge-m3", "input": ["What is ColBERT?"], "truncate": true, "keep_alive": "30m"}"""),
                ),
        )
    }

    @Test
    fun `embedding of the wrong dimension is a malformed body`() {
        WireMockFixture.stubEmbed(FloatArray(3))
        assertThrows<MalformedResponseException> { EmbeddingClient(properties, http, guards).embed("x") }
    }

    @Test
    fun `route call is structured json with num_predict 32, temperature 0 and seed 42`() {
        WireMockFixture.stubRoute("sql")
        val reply = ollama.chat(LlmUse.ROUTE, "system text", "question")
        assertEquals("""{"route":"sql"}""", reply.content)
        assertEquals(50, reply.promptTokens)
        assertEquals(3, reply.completionTokens)
        server.verify(postRequestedFor(urlEqualTo("/api/chat")).withRequestBody(equalToJson(chatBody(format = true, numPredict = 32))))
    }

    @Test
    fun `sql generation call is structured json with num_predict 256`() {
        WireMockFixture.stubSqlGen("select 1")
        ollama.chat(LlmUse.SQL_GENERATION, "system text", "question")
        server.verify(postRequestedFor(urlEqualTo("/api/chat")).withRequestBody(equalToJson(chatBody(format = true, numPredict = 256))))
    }

    @Test
    fun `answer call omits format and uses num_predict 384`() {
        server.stubFor(post(urlEqualTo("/api/chat")).willReturn(WireMockFixture.chat("an answer [1]", 900, 60)))
        val reply = ollama.chat(LlmUse.ANSWER, "system text", "question")
        assertEquals(900, reply.promptTokens)
        assertEquals(60, reply.completionTokens)
        server.verify(
            postRequestedFor(urlEqualTo("/api/chat"))
                .withRequestBody(equalToJson(chatBody(format = false, numPredict = 384)))
                .withRequestBody(matchingJsonPath("$.format", absent())),
        )
    }

    @Test
    fun `rerank request carries string ids, texts and top_k`() {
        WireMockFixture.stubRerankReversing()
        val scores = RerankerClient(properties, http, guards).rerank("q", listOf(RerankPassage(48213, "a"), RerankPassage(48190, "b")))
        assertEquals(mapOf(48213L to 1.0 / 3, 48190L to 2.0 / 3), scores)
        server.verify(
            postRequestedFor(urlEqualTo("/rerank")).withRequestBody(
                equalToJson("""{"query": "q", "passages": [{"id": "48213", "text": "a"}, {"id": "48190", "text": "b"}], "top_k": 2}"""),
            ),
        )
    }

    @Test
    fun `anthropic client sends the messages request with headers and reads usage`() {
        server.stubFor(
            post(urlEqualTo("/v1/messages")).willReturn(
                WireMockFixture.json(
                    mapOf(
                        "content" to listOf(mapOf("type" to "text", "text" to "{\"route\": \"vector\"}")),
                        "usage" to mapOf("input_tokens" to 11, "output_tokens" to 4),
                    ),
                ),
            ),
        )
        val reply = AnthropicLlmClient(properties, http, guards).chat(LlmUse.ROUTE, "sys", "q")
        assertEquals("{\"route\": \"vector\"}", reply.content)
        assertEquals(11, reply.promptTokens)
        assertEquals(4, reply.completionTokens)
        server.verify(
            postRequestedFor(urlEqualTo("/v1/messages"))
                .withHeader("x-api-key", equalTo("test-key"))
                .withHeader("anthropic-version", equalTo("2023-06-01"))
                .withRequestBody(
                    equalToJson(
                        """{"model": "claude-test", "max_tokens": 32, "temperature": 0, "system": "sys",
                           "messages": [{"role": "user", "content": "q"}]}""",
                    ),
                ),
        )
    }

    @Test
    fun `context fails to start with provider anthropic and no key`() {
        val error =
            assertThrows<Exception> {
                start("--rag.anthropic.api-key=", "--rag.anthropic.model=claude-test")
            }
        val messages = generateSequence<Throwable>(error) { it.cause }.mapNotNull { it.message }.joinToString(" | ")
        assertTrue("ANTHROPIC_API_KEY" in messages, messages)
    }

    @Test
    fun `context fails to start with provider anthropic and no model`() {
        val error =
            assertThrows<Exception> {
                start("--rag.anthropic.api-key=key", "--rag.anthropic.model=")
            }
        val messages = generateSequence<Throwable>(error) { it.cause }.mapNotNull { it.message }.joinToString(" | ")
        assertTrue("ANTHROPIC_MODEL" in messages, messages)
    }

    /** Command-line args outrank application.yml; the DB URL points nowhere so no real database is touched. */
    private fun start(vararg args: String) =
        SpringApplicationBuilder(Application::class.java)
            .run(
                "--spring.main.web-application-type=none",
                "--spring.flyway.enabled=false",
                "--rag.db.url=jdbc:postgresql://127.0.0.1:1/none",
                "--rag.llm.provider=anthropic",
                *args,
            ).close()

    private fun chatBody(
        format: Boolean,
        numPredict: Int,
    ): String {
        val formatField = if (format) """"format": "json",""" else ""
        return """
            {"model": "llama3.1:8b",
             "messages": [{"role": "system", "content": "system text"}, {"role": "user", "content": "question"}],
             "stream": false, $formatField "keep_alive": "30m",
             "options": {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": $numPredict}}
            """.trimIndent()
    }
}
