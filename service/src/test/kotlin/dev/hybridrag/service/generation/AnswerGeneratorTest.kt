package dev.hybridrag.service.generation

import dev.hybridrag.service.resilience.Dependency
import dev.hybridrag.service.resilience.DependencyHttpException
import io.github.resilience4j.circuitbreaker.CallNotPermittedException
import io.github.resilience4j.circuitbreaker.CircuitBreaker
import org.junit.jupiter.api.Test
import org.springframework.web.client.ResourceAccessException
import java.net.http.HttpTimeoutException
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** Task 6.1 (unit): citation mapping, abstention and failure classification of design D8. */
class AnswerGeneratorTest {
    private val ids = listOf(101L, 102L, 103L, 104L, 105L, 106L)

    @Test
    fun `citations map to chunk ids in first-mention order without duplicates`() {
        assertEquals(listOf(102L, 101L), Citations.parse("ColBERT uses late interaction [2][1][2].", ids))
    }

    @Test
    fun `out-of-range citations are ignored`() {
        assertEquals(emptyList(), Citations.parse("See [9] and [0].", ids))
        assertEquals(listOf(106L), Citations.parse("[7] [6]", ids))
    }

    @Test
    fun `citations are limited to the passages actually sent`() {
        assertEquals(listOf(101L), Citations.parse("[1] [2]", ids.take(1)))
    }

    @Test
    fun `abstention is detected only at the start of the answer`() {
        assertTrue(AnswerGenerator.isAbstention("I cannot answer this from the indexed papers."))
        assertTrue(AnswerGenerator.isAbstention("  I cannot answer this from the indexed papers. Sorry."))
        assertFalse(AnswerGenerator.isAbstention("ColBERT [1]. I cannot answer this from the indexed papers."))
        assertFalse(AnswerGenerator.isAbstention("I cannot answer this."))
    }

    @Test
    fun `answer prompt numbers passages with title and section path and ends with the question`() {
        val prompt =
            AnswerGenerator.answerUser(
                "What is ColBERT?",
                listOf(
                    chunk(1, "Title A", "1 Intro", "alpha"),
                    chunk(2, "Title B", "2 Method > 2.1 Model", "beta"),
                ),
            )
        assertEquals(
            "Passages:\n[1] Title A — 1 Intro\nalpha\n\n[2] Title B — 2 Method > 2.1 Model\nbeta\n\nQuestion: What is ColBERT?",
            prompt,
        )
        assertTrue(AnswerGenerator.ANSWER_SYSTEM.endsWith("reply exactly: I cannot answer this from the indexed papers."))
    }

    @Test
    fun `unreachable, open breaker, timeout and 5xx are unavailable 503, other failures are error 500`() {
        val breaker = CircuitBreaker.ofDefaults("llm")
        listOf(
            ResourceAccessException("refused", java.net.ConnectException("refused")),
            CallNotPermittedException.createCallNotPermittedException(breaker),
            ResourceAccessException("timeout", HttpTimeoutException("timed out")),
            DependencyHttpException(Dependency.LLM, 503),
        ).forEach {
            val answer = AnswerGenerator.failure(it)
            assertEquals("unavailable", answer.status)
            assertEquals(503, answer.httpStatus)
        }
        val error = AnswerGenerator.failure(DependencyHttpException(Dependency.LLM, 404))
        assertEquals("error", error.status)
        assertEquals(500, error.httpStatus)
    }

    private fun chunk(
        id: Long,
        title: String,
        section: String,
        content: String,
    ) = dev.hybridrag.service.api.ChunkResponse(
        rank = id.toInt(),
        chunkId = id,
        externalId = "x",
        ordinal = 0,
        title = title,
        sectionPath = section,
        publishedOn = java.time.LocalDate.of(2024, 1, 1),
        primaryCategory = "cs.IR",
        content = content,
        contextHeader = "HEADER",
        scores =
            dev.hybridrag.service.api
                .ChunkScores(),
    )
}
