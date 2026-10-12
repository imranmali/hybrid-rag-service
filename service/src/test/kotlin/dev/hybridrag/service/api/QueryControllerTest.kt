package dev.hybridrag.service.api

import dev.hybridrag.service.observability.TracingConfiguration
import dev.hybridrag.service.pipeline.QueryPipeline
import org.junit.jupiter.api.Test
import org.mockito.ArgumentMatchers.any
import org.mockito.Mockito.`when`
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest
import org.springframework.context.annotation.Import
import org.springframework.http.MediaType
import org.springframework.test.context.bean.override.mockito.MockitoBean
import org.springframework.test.web.servlet.MockMvc
import org.springframework.test.web.servlet.post
import java.time.LocalDate

/** Task 2.2: `POST /v1/query` and the problem+json mapping of design D11, with a stub pipeline. */
@WebMvcTest(QueryController::class)
@Import(TracingConfiguration::class)
class QueryControllerTest {
    @Autowired
    lateinit var mvc: MockMvc

    @MockitoBean
    lateinit var pipeline: QueryPipeline

    private val traceIdPattern = "^[0-9a-f]{32}$"

    @Test
    fun `served request has the X-Trace-Id header and the degraded header`() {
        `when`(pipeline.query(anyRequest())).thenReturn(response(degraded = listOf("rerank_unavailable", "llm_unavailable")))
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"question": "What is ColBERT?"}"""
            }.andExpect {
                status { isOk() }
                header { string("X-Trace-Id", matchesRegex(traceIdPattern)) }
                header { string("X-RAG-Degraded", "rerank_unavailable,llm_unavailable") }
                jsonPath("$.trace_id") { value("4bf92f3577b34da6a3ce929d0e0e4736") }
                jsonPath("$.timings_ms.total") { exists() }
                jsonPath("$.chunks[0].scores.fused") { value(nullValue()) }
                jsonPath("$.chunks[0].published_on") { value("2024-03-02") }
            }
    }

    @Test
    fun `no degraded header when nothing degraded`() {
        `when`(pipeline.query(anyRequest())).thenReturn(response(degraded = emptyList()))
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"question": "What is ColBERT?"}"""
            }.andExpect {
                status { isOk() }
                header { doesNotExist("X-RAG-Degraded") }
            }
    }

    @Test
    fun `validation error is a 400 problem with errors and trace_id`() {
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"question": "x", "pipeline": {"retrieval": {"vector": false, "bm25": false}}}"""
            }.andExpect {
                status { isBadRequest() }
                content { contentTypeCompatibleWith(MediaType.APPLICATION_PROBLEM_JSON) }
                header { string("X-Trace-Id", matchesRegex(traceIdPattern)) }
                jsonPath("$.type") { value("about:blank") }
                jsonPath("$.title") { value("Bad Request") }
                jsonPath("$.status") { value(400) }
                jsonPath("$.detail") { isString() }
                jsonPath("$.errors[0].field") { value("pipeline.retrieval") }
                jsonPath("$.errors[0].message") { isString() }
                jsonPath("$.trace_id") { value(matchesRegex(traceIdPattern)) }
            }
    }

    @Test
    fun `misspelled field is a 400 naming the unknown field`() {
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"question": "x", "pipeline": {"rerank": {"enable": true}}}"""
            }.andExpect {
                status { isBadRequest() }
                content { contentTypeCompatibleWith(MediaType.APPLICATION_PROBLEM_JSON) }
                jsonPath("$.errors[0].field") { value("pipeline.rerank.enable") }
                jsonPath("$.trace_id") { value(matchesRegex(traceIdPattern)) }
            }
    }

    @Test
    fun `non-ISO date and missing question are 400`() {
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"question": "x", "pipeline": {"filters": {"explicit": {"published_from": "2024/01/01"}}}}"""
            }.andExpect {
                status { isBadRequest() }
                jsonPath("$.errors[0].field") { value("pipeline.filters.explicit.published_from") }
            }
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"pipeline": {}}"""
            }.andExpect { status { isBadRequest() } }
    }

    @Test
    fun `retrieval unavailable maps to 503 problem`() {
        `when`(pipeline.query(anyRequest())).thenThrow(RetrievalUnavailableException("database unavailable"))
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"question": "What is ColBERT?"}"""
            }.andExpect {
                status { isServiceUnavailable() }
                content { contentTypeCompatibleWith(MediaType.APPLICATION_PROBLEM_JSON) }
                jsonPath("$.status") { value(503) }
                jsonPath("$.trace_id") { value(matchesRegex(traceIdPattern)) }
            }
    }

    @Test
    fun `unexpected error maps to 500 problem`() {
        `when`(pipeline.query(anyRequest())).thenThrow(IllegalStateException("boom"))
        mvc
            .post("/v1/query") {
                contentType = MediaType.APPLICATION_JSON
                content = """{"question": "What is ColBERT?"}"""
            }.andExpect {
                status { isInternalServerError() }
                jsonPath("$.status") { value(500) }
                jsonPath("$.trace_id") { value(matchesRegex(traceIdPattern)) }
            }
    }

    private fun response(degraded: List<String>) =
        QueryResponse(
            requestId = "6f1c2a0e-3d1b-4c0a-9a55-2f8f0e7b9b11",
            traceId = "4bf92f3577b34da6a3ce929d0e0e4736",
            route = "vector",
            routeSource = "llm",
            pipeline = PipelineDefaults.PIPELINE,
            filters = null,
            chunks =
                listOf(
                    ChunkResponse(
                        1,
                        48213,
                        "2403.01234",
                        7,
                        "t",
                        "4 Experiments",
                        LocalDate.of(2024, 3, 2),
                        "cs.IR",
                        "c",
                        "h",
                        ChunkScores(vector = 0.8, vectorRank = 1),
                    ),
                ),
            sql = null,
            answer = AnswerResponse.DISABLED,
            degraded = degraded,
            timingsMs = mapOf("total" to 1.0),
            usage = Usage(),
        )

    /** Mockito's `any()` returns null, which Kotlin rejects for a non-null parameter. */
    private fun anyRequest(): QueryRequest = any(QueryRequest::class.java) ?: QueryRequest("matcher")

    private fun matchesRegex(pattern: String) = org.hamcrest.Matchers.matchesRegex(pattern)

    private fun nullValue() = org.hamcrest.Matchers.nullValue()
}
