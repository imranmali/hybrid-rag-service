package dev.hybridrag.service.api

import com.fasterxml.jackson.databind.ObjectMapper
import com.fasterxml.jackson.databind.PropertyNamingStrategies
import com.fasterxml.jackson.databind.exc.UnrecognizedPropertyException
import com.fasterxml.jackson.module.kotlin.readValue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertDoesNotThrow
import org.junit.jupiter.api.assertThrows
import org.springframework.http.converter.json.Jackson2ObjectMapperBuilder
import java.time.LocalDate
import kotlin.test.assertEquals
import kotlin.test.assertNull

/** Task 2.1: contract C6 defaults and one test per validation rule. */
class ValidationTest {
    private val mapper: ObjectMapper =
        Jackson2ObjectMapperBuilder
            .json()
            .propertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
            .failOnUnknownProperties(true)
            .build()

    private fun parse(json: String): QueryRequest = mapper.readValue(json)

    @Test
    fun `minimal request resolves to the full-pipeline defaults`() {
        val pipeline = PipelineDefaults.resolve(parse("""{"question": "What is ColBERT?"}"""))
        assertEquals(PipelineDefaults.PIPELINE, pipeline)
        with(pipeline) {
            assertEquals(true, retrieval.vector)
            assertEquals(true, retrieval.bm25)
            assertEquals(50, retrieval.vectorK)
            assertEquals(50, retrieval.bm25K)
            assertEquals("rrf", retrieval.fusion.method)
            assertEquals(60, retrieval.fusion.k)
            assertEquals(50, retrieval.returnK)
            assertEquals(true, contextual)
            assertEquals(true, filters.enabled)
            assertEquals(true, filters.extractFromQuestion)
            assertEquals("auto", filters.strategy)
            assertEquals(0.25, filters.selectivityThreshold)
            assertEquals(5, filters.overfetchFactor)
            assertNull(filters.explicit)
            assertEquals(true, rerank.enabled)
            assertEquals(20, rerank.candidates)
            assertEquals(true, routing.enabled)
            assertEquals(true, generation.enabled)
            assertEquals(6, generation.contextK)
        }
    }

    @Test
    fun `a partial pipeline keeps every other default`() {
        val pipeline = parse("""{"question": "x", "pipeline": {"rerank": {"enabled": false}}}""").pipeline
        assertEquals(PipelineDefaults.PIPELINE.copy(rerank = RerankSettings(enabled = false)), pipeline)
    }

    @Test
    fun `explicit null is allowed and explicit values bind from snake_case`() {
        assertNull(parse("""{"question": "x", "pipeline": {"filters": {"explicit": null}}}""").pipeline.filters.explicit)
        val explicit =
            parse(
                """{"question": "x", "pipeline": {"filters": {"explicit": {"categories": ["cs.IR"], "authors": ["Jimmy Lin"],
                   "published_from": "2024-01-01", "published_to": "2024-12-31"}}}}""",
            ).pipeline.filters.explicit!!
        assertEquals(FilterValues(listOf("cs.IR"), listOf("Jimmy Lin"), LocalDate.of(2024, 1, 1), LocalDate.of(2024, 12, 31)), explicit)
    }

    @Test
    fun `unknown nested field is rejected by the strict mapper`() {
        val error = assertThrows<UnrecognizedPropertyException> { parse("""{"question": "x", "pipeline": {"rerank": {"enable": true}}}""") }
        assertEquals("enable", error.propertyName)
    }

    @Test
    fun `default request is valid`() = assertDoesNotThrow { QueryValidator.validate(QueryRequest("What is ColBERT?")) }

    @Test
    fun `blank question`() = rejects("question", QueryRequest("   "))

    @Test
    fun `question over 2000 characters`() = rejects("question", QueryRequest("x".repeat(2001)))

    @Test
    fun `both retrieval legs disabled`() = rejects("pipeline.retrieval", retrieval(RetrievalSettings(vector = false, bm25 = false)))

    @Test
    fun `vector_k out of range`() = rejects("pipeline.retrieval.vector_k", retrieval(RetrievalSettings(vectorK = 0)))

    @Test
    fun `bm25_k out of range`() = rejects("pipeline.retrieval.bm25_k", retrieval(RetrievalSettings(bm25K = 201)))

    @Test
    fun `return_k out of range`() = rejects("pipeline.retrieval.return_k", retrieval(RetrievalSettings(returnK = 201)))

    @Test
    fun `fusion method other than rrf`() =
        rejects("pipeline.retrieval.fusion.method", retrieval(RetrievalSettings(fusion = FusionSettings(method = "linear"))))

    @Test
    fun `fusion k out of range`() = rejects("pipeline.retrieval.fusion.k", retrieval(RetrievalSettings(fusion = FusionSettings(k = 1001))))

    @Test
    fun `unknown filter strategy`() = rejects("pipeline.filters.strategy", filters(FilterSettings(strategy = "bogus")))

    @Test
    fun `selectivity threshold out of range`() =
        rejects("pipeline.filters.selectivity_threshold", filters(FilterSettings(selectivityThreshold = 1.5)))

    @Test
    fun `overfetch factor out of range`() = rejects("pipeline.filters.overfetch_factor", filters(FilterSettings(overfetchFactor = 21)))

    @Test
    fun `vector_k times overfetch factor over 1000`() =
        rejects(
            "pipeline.filters.overfetch_factor",
            QueryRequest("x", Pipeline(retrieval = RetrievalSettings(vectorK = 200), filters = FilterSettings(overfetchFactor = 6))),
        )

    @Test
    fun `malformed explicit category`() =
        rejects(
            "pipeline.filters.explicit.categories[1]",
            filters(FilterSettings(explicit = FilterValues(categories = listOf("cs.IR", "CS IR")))),
        )

    @Test
    fun `reversed date range`() =
        rejects(
            "pipeline.filters.explicit.published_from",
            filters(
                FilterSettings(explicit = FilterValues(publishedFrom = LocalDate.of(2025, 1, 1), publishedTo = LocalDate.of(2024, 1, 1))),
            ),
        )

    @Test
    fun `rerank candidates out of range`() =
        rejects("pipeline.rerank.candidates", QueryRequest("x", Pipeline(rerank = RerankSettings(candidates = 0))))

    @Test
    fun `rerank candidates above return_k`() =
        rejects(
            "pipeline.rerank.candidates",
            QueryRequest("x", Pipeline(retrieval = RetrievalSettings(returnK = 10), rerank = RerankSettings(candidates = 11))),
        )

    @Test
    fun `context_k out of range`() =
        rejects("pipeline.generation.context_k", QueryRequest("x", Pipeline(generation = GenerationSettings(contextK = 21))))

    @Test
    fun `every violation is reported`() {
        val error =
            assertThrows<RequestValidationException> {
                QueryValidator.validate(QueryRequest("", Pipeline(retrieval = RetrievalSettings(vector = false, bm25 = false))))
            }
        assertEquals(listOf("question", "pipeline.retrieval"), error.errors.map { it.field })
    }

    private fun retrieval(settings: RetrievalSettings) = QueryRequest("x", Pipeline(retrieval = settings))

    private fun filters(settings: FilterSettings) = QueryRequest("x", Pipeline(filters = settings))

    private fun rejects(
        field: String,
        request: QueryRequest,
    ) {
        val error = assertThrows<RequestValidationException> { QueryValidator.validate(request) }
        assertEquals(listOf(field), error.errors.map { it.field }.distinct(), error.message)
    }
}
