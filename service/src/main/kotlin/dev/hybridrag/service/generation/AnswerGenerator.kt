package dev.hybridrag.service.generation

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.hybridrag.service.api.AnswerResponse
import dev.hybridrag.service.api.ChunkResponse
import dev.hybridrag.service.api.SqlResult
import dev.hybridrag.service.llm.LlmClient
import dev.hybridrag.service.llm.LlmReply
import dev.hybridrag.service.llm.LlmUse
import dev.hybridrag.service.resilience.DependencyHttpException
import dev.hybridrag.service.resilience.RetryPolicy
import io.github.resilience4j.circuitbreaker.CallNotPermittedException
import org.springframework.stereotype.Component
import org.springframework.web.client.ResourceAccessException
import java.io.IOException

data class GeneratedAnswer(
    val answer: AnswerResponse,
    val reply: LlmReply,
)

/** Grounded answers with citations (`answer-v1`) and SQL-row answers (`sqlanswer-v1`), design D8. */
@Component
class AnswerGenerator(
    private val llm: LlmClient,
) {
    private val mapper = jacksonObjectMapper()

    /** Answers from the given (already `context_k`-truncated) chunks; the prompt does not depend on `contextual`. */
    fun fromChunks(
        question: String,
        passages: List<ChunkResponse>,
    ): GeneratedAnswer {
        val reply = llm.chat(LlmUse.ANSWER, ANSWER_SYSTEM, answerUser(question, passages))
        val text = reply.content
        val answer = AnswerResponse("ok", 200, text, isAbstention(text), Citations.parse(text, passages.map { it.chunkId }), null)
        return GeneratedAnswer(answer, reply)
    }

    fun fromSql(
        question: String,
        sql: SqlResult,
    ): GeneratedAnswer {
        val user =
            """
            Question: $question
            SQL: ${sql.query}
            Columns: ${mapper.writeValueAsString(sql.columns)}
            Rows: ${mapper.writeValueAsString(sql.rows)}

            Answer concisely using only these rows.
            """.trimIndent()
        val reply = llm.chat(LlmUse.ANSWER, SQL_ANSWER_SYSTEM, user)
        return GeneratedAnswer(AnswerResponse("ok", 200, reply.content, isAbstention(reply.content), emptyList(), null), reply)
    }

    companion object {
        const val ANSWER_VERSION = "answer-v1"
        const val SQL_ANSWER_VERSION = "sqlanswer-v1"
        const val ABSTENTION = "I cannot answer this from the indexed papers."

        const val ANSWER_SYSTEM =
            "You answer questions about arXiv papers using ONLY the numbered passages. Cite every claim with the passage " +
                "number in square brackets, e.g. [2]. If the passages do not contain the answer, reply exactly: $ABSTENTION"

        const val SQL_ANSWER_SYSTEM =
            "You answer questions about arXiv paper metadata using ONLY the rows returned by a SQL query. " +
                "If the rows do not contain the answer, reply exactly: $ABSTENTION"

        fun answerUser(
            question: String,
            passages: List<ChunkResponse>,
        ): String {
            val numbered =
                passages
                    .mapIndexed { index, chunk -> "[${index + 1}] ${chunk.title} — ${chunk.sectionPath}\n${chunk.content}" }
                    .joinToString("\n\n")
            return "Passages:\n$numbered\n\nQuestion: $question"
        }

        fun isAbstention(text: String) = text.trim().startsWith(ABSTENTION)

        /** `unavailable` (503) when the LLM is unreachable, its breaker is open or it timed out; otherwise `error` (500). */
        fun failure(error: Throwable): AnswerResponse {
            val message = error.message ?: error.javaClass.simpleName
            val unavailable =
                error is CallNotPermittedException ||
                    RetryPolicy.isTimeout(error) ||
                    (error is DependencyHttpException && error.status >= 500) ||
                    error is ResourceAccessException ||
                    generateSequence(error) { it.cause }.any { it is IOException }
            return if (unavailable) AnswerResponse.unavailable(message) else AnswerResponse.error(message)
        }
    }
}

object Citations {
    private val marker = Regex("\\[(\\d+)]")

    /** `[n]` markers → chunk ids of passages 1..size, first-mention order, no duplicates; out-of-range ignored. */
    fun parse(
        text: String,
        passageIds: List<Long>,
    ): List<Long> =
        marker
            .findAll(text)
            .mapNotNull { it.groupValues[1].toIntOrNull() }
            .filter { it in 1..passageIds.size }
            .map { passageIds[it - 1] }
            .distinct()
            .toList()
}
