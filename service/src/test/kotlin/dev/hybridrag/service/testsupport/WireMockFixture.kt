package dev.hybridrag.service.testsupport

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.github.tomakehurst.wiremock.WireMockServer
import com.github.tomakehurst.wiremock.client.MappingBuilder
import com.github.tomakehurst.wiremock.client.ResponseDefinitionBuilder
import com.github.tomakehurst.wiremock.client.WireMock.aResponse
import com.github.tomakehurst.wiremock.client.WireMock.containing
import com.github.tomakehurst.wiremock.client.WireMock.equalTo
import com.github.tomakehurst.wiremock.client.WireMock.get
import com.github.tomakehurst.wiremock.client.WireMock.matchingJsonPath
import com.github.tomakehurst.wiremock.client.WireMock.post
import com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor
import com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo
import com.github.tomakehurst.wiremock.core.WireMockConfiguration.wireMockConfig
import com.github.tomakehurst.wiremock.extension.ResponseDefinitionTransformerV2
import com.github.tomakehurst.wiremock.http.ResponseDefinition
import com.github.tomakehurst.wiremock.matching.RequestPatternBuilder
import com.github.tomakehurst.wiremock.stubbing.Scenario
import com.github.tomakehurst.wiremock.stubbing.ServeEvent

/** One WireMock server (dynamic port) standing in for Ollama and the reranker (design D13). */
object WireMockFixture {
    private val mapper = jacksonObjectMapper()

    val server: WireMockServer by lazy {
        WireMockServer(
            wireMockConfig()
                .dynamicPort()
                .containerThreads(64)
                .asynchronousResponseEnabled(true)
                .asynchronousResponseThreads(32)
                .extensions(ReverseRerank()),
        ).also { it.start() }
    }

    val baseUrl: String get() = server.baseUrl()

    // ---- request patterns ----
    val embedRequests: RequestPatternBuilder get() = postRequestedFor(urlEqualTo("/api/embed"))
    val rerankRequests: RequestPatternBuilder get() = postRequestedFor(urlEqualTo("/rerank"))
    val routeRequests: RequestPatternBuilder
        get() = postRequestedFor(urlEqualTo("/api/chat")).withRequestBody(matchingJsonPath("$.options.num_predict", equalTo("32")))
    val sqlGenRequests: RequestPatternBuilder
        get() = postRequestedFor(urlEqualTo("/api/chat")).withRequestBody(matchingJsonPath("$.options.num_predict", equalTo("256")))
    val answerRequests: RequestPatternBuilder
        get() = postRequestedFor(urlEqualTo("/api/chat")).withRequestBody(ANSWER_PROMPT)
    val chatRequests: RequestPatternBuilder get() = postRequestedFor(urlEqualTo("/api/chat"))

    private val ANSWER_PROMPT = matchingJsonPath("$.messages[0].content", containing("numbered passages"))
    private val SQL_ANSWER_PROMPT = matchingJsonPath("$.messages[0].content", containing("rows returned by a SQL query"))

    fun count(pattern: RequestPatternBuilder): Int = server.countRequestsMatching(pattern.build()).count

    // ---- stubs ----
    fun embedReturns(vector: FloatArray): MappingBuilder =
        post(urlEqualTo("/api/embed")).willReturn(
            json(
                mapOf(
                    "model" to "bge-m3",
                    "embeddings" to listOf(vector.toList()),
                    "prompt_eval_count" to 7,
                ),
            ),
        )

    fun stubEmbed(vector: FloatArray) = server.stubFor(embedReturns(vector))

    fun stubRoute(route: String) = stubRouteRaw(mapper.writeValueAsString(mapOf("route" to route)))

    fun stubRouteRaw(content: String) =
        server.stubFor(
            post(urlEqualTo("/api/chat"))
                .withRequestBody(matchingJsonPath("$.options.num_predict", equalTo("32")))
                .willReturn(chat(content, 50, 3)),
        )

    fun stubSqlGen(sql: String) =
        server.stubFor(
            post(urlEqualTo("/api/chat"))
                .withRequestBody(matchingJsonPath("$.options.num_predict", equalTo("256")))
                .willReturn(chat(mapper.writeValueAsString(mapOf("sql" to sql)), 300, 40)),
        )

    fun stubAnswer(
        text: String,
        response: ResponseDefinitionBuilder = chat(text, 900, 60),
    ) = server.stubFor(post(urlEqualTo("/api/chat")).withRequestBody(ANSWER_PROMPT).willReturn(response))

    fun stubSqlAnswer(text: String) =
        server.stubFor(post(urlEqualTo("/api/chat")).withRequestBody(SQL_ANSWER_PROMPT).willReturn(chat(text, 200, 20)))

    /** The reranker scores passage i of n as (i + 1) / (n + 1), i.e. it reverses the order it was given. */
    fun stubRerankReversing() = server.stubFor(post(urlEqualTo("/rerank")).willReturn(aResponse().withTransformers(ReverseRerank.NAME)))

    fun stubRerankStatus(status: Int) = server.stubFor(post(urlEqualTo("/rerank")).willReturn(aResponse().withStatus(status)))

    /** First call answers 503, every later call is scored normally. */
    fun stubRerank503Then200() {
        server.stubFor(
            post(urlEqualTo("/rerank"))
                .inScenario("flaky")
                .whenScenarioStateIs(Scenario.STARTED)
                .willReturn(aResponse().withStatus(503).withBody("{\"detail\": \"model not ready\"}"))
                .willSetStateTo("recovered"),
        )
        server.stubFor(
            post(urlEqualTo("/rerank"))
                .inScenario("flaky")
                .whenScenarioStateIs("recovered")
                .willReturn(aResponse().withTransformers(ReverseRerank.NAME)),
        )
    }

    fun stubTags(
        embedDigest: String,
        llmDigest: String = "2".repeat(64),
    ) = server.stubFor(
        get(urlEqualTo("/api/tags")).willReturn(
            json(
                mapOf(
                    "models" to
                        listOf(
                            mapOf("name" to "bge-m3:latest", "model" to "bge-m3:latest", "digest" to embedDigest),
                            mapOf("name" to "llama3.1:8b", "model" to "llama3.1:8b", "digest" to llmDigest),
                        ),
                ),
            ),
        ),
    )

    fun stubRerankerHealth() =
        server.stubFor(
            get(urlEqualTo("/healthz")).willReturn(
                json(mapOf("status" to "ok", "model" to "BAAI/bge-reranker-v2-m3", "revision" to "abc123", "ready" to true)),
            ),
        )

    fun chat(
        content: String,
        promptTokens: Int,
        completionTokens: Int,
    ): ResponseDefinitionBuilder =
        json(
            mapOf(
                "message" to mapOf("role" to "assistant", "content" to content),
                "prompt_eval_count" to promptTokens,
                "eval_count" to completionTokens,
                "done" to true,
            ),
        )

    fun json(body: Any): ResponseDefinitionBuilder =
        aResponse().withStatus(200).withHeader("content-type", "application/json").withBody(mapper.writeValueAsString(body))

    /** Request bodies of the matching requests, in arrival order. */
    fun bodies(pattern: RequestPatternBuilder): List<String> = server.findAll(pattern).map { it.bodyAsString }

    class ReverseRerank : ResponseDefinitionTransformerV2 {
        override fun getName() = NAME

        override fun applyGlobally() = false

        override fun transform(serveEvent: ServeEvent): ResponseDefinition {
            val passages = mapper.readTree(serveEvent.request.bodyAsString).path("passages")
            val n = passages.size()
            val results =
                passages
                    .mapIndexed { index, passage ->
                        mapOf(
                            "id" to passage.path("id").asText(),
                            "index" to index,
                            "score" to (index + 1.0) / (n + 1),
                        )
                    }.sortedByDescending { it["score"] as Double }
            return ResponseDefinition
                .okForJson(mapOf("model" to "fake", "results" to results, "latency_ms" to 1.0))
        }

        companion object {
            const val NAME = "reverse-rerank"
        }
    }
}
