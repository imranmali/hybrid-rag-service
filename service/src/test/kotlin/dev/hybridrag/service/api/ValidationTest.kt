package dev.hybridrag.service.api

import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertDoesNotThrow
import org.junit.jupiter.api.assertThrows
import java.time.LocalDate
import kotlin.test.assertEquals

class ValidationTest {
    @Test
    fun `default request is valid`() {
        assertDoesNotThrow { QueryValidator.validate(QueryRequest(question = "What is retrieval?")) }
        assertEquals(
            60,
            PipelineDefaults
                .resolve(QueryRequest(question = "x"))
                .retrieval.fusion.k,
        )
    }

    @Test
    fun `blank question is rejected`() = rejects(QueryRequest(question = " "))

    @Test
    fun `both retrieval legs disabled is rejected`() =
        rejects(QueryRequest(question = "x", pipeline = Pipeline(retrieval = RetrievalSettings(vector = false, bm25 = false))))

    @Test
    fun `invalid limits are rejected`() =
        rejects(QueryRequest(question = "x", pipeline = Pipeline(retrieval = RetrievalSettings(vectorK = 201))))

    @Test
    fun `invalid filter strategy is rejected`() =
        rejects(QueryRequest(question = "x", pipeline = Pipeline(filters = FilterSettings(strategy = "bogus"))))

    @Test
    fun `reversed filter dates are rejected`() =
        rejects(
            QueryRequest(
                question = "x",
                pipeline =
                    Pipeline(
                        filters =
                            FilterSettings(
                                explicit =
                                    ExplicitFilters(
                                        publishedFrom = LocalDate.parse("2025-01-01"),
                                        publishedTo = LocalDate.parse("2024-01-01"),
                                    ),
                            ),
                    ),
            ),
        )

    @Test
    fun `rerank candidates cannot exceed return k`() =
        rejects(
            QueryRequest(
                question = "x",
                pipeline = Pipeline(retrieval = RetrievalSettings(returnK = 10), rerank = RerankSettings(candidates = 11)),
            ),
        )

    private fun rejects(request: QueryRequest) {
        assertThrows<RequestValidationException> { QueryValidator.validate(request) }
    }
}
