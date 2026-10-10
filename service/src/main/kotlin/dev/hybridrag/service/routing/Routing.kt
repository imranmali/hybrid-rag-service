package dev.hybridrag.service.routing

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import dev.hybridrag.service.llm.LlmClient
import dev.hybridrag.service.llm.LlmReply
import dev.hybridrag.service.llm.LlmUse
import org.springframework.stereotype.Component

private val replyMapper = jacksonObjectMapper()

/** A structured LLM reply that is not the expected JSON object. */
class UnparseableReplyException(
    message: String,
) : RuntimeException(message)

data class RouteDecision(
    val route: String,
    val reply: LlmReply,
)

/** `route-v1` classifier (design D7): `{"route": "sql"|"vector"}`. */
@Component
class RouteClassifier(
    private val llm: LlmClient,
) {
    fun classify(question: String): RouteDecision {
        val reply = llm.chat(LlmUse.ROUTE, SYSTEM, question)
        val route =
            runCatching { replyMapper.readTree(reply.content).path("route").asText() }
                .getOrNull()
                ?.takeIf { it == "sql" || it == "vector" }
                ?: throw UnparseableReplyException("route classifier reply is not {\"route\": \"sql\"|\"vector\"}")
        return RouteDecision(route, reply)
    }

    companion object {
        const val VERSION = "route-v1"

        val SYSTEM =
            """
            You route questions about a collection of arXiv computer-science papers.
            Answer "sql" when the question asks to count, list, rank or aggregate papers using only their metadata:
            titles, authors, categories (for example cs.IR, cs.CL, cs.DB) and publication dates. Such questions do not
            need to read what the papers say.
            Answer "vector" for anything about what papers say, propose, find, compare or evaluate.
            Reply with only a JSON object: {"route": "sql"} or {"route": "vector"}.

            Examples:
            Q: How many cs.CL papers were published in 2025? -> {"route": "sql"}
            Q: List the titles of papers co-authored by Jimmy Lin. -> {"route": "sql"}
            Q: Which author has the most cs.IR papers since 2024? -> {"route": "sql"}
            Q: Which reranking models are evaluated in the indexed cs.IR papers? -> {"route": "vector"}
            Q: How does ColBERT's late interaction differ from a cross-encoder? -> {"route": "vector"}
            Q: What datasets do the 2024 retrieval-augmented generation papers use for evaluation? -> {"route": "vector"}
            """.trimIndent()
    }
}

data class GeneratedSql(
    val sql: String,
    val reply: LlmReply,
)

/** `sqlgen-v1` generator (design D7): `{"sql": "…"}` over the tables and columns granted to `rag_router`. */
@Component
class SqlGenerator(
    private val llm: LlmClient,
) {
    fun generate(question: String): GeneratedSql {
        val reply = llm.chat(LlmUse.SQL_GENERATION, SYSTEM, question)
        val sql =
            runCatching { replyMapper.readTree(reply.content).path("sql").asText() }
                .getOrNull()
                ?.takeIf { it.isNotBlank() }
                ?: throw UnparseableReplyException("SQL generator reply is not {\"sql\": \"…\"}")
        return GeneratedSql(sql, reply)
    }

    companion object {
        const val VERSION = "sqlgen-v1"

        val SYSTEM =
            """
            You write one PostgreSQL query that answers a question about arXiv paper metadata.
            Schema (the only tables and columns you may use):
              documents(id uuid, external_id text, arxiv_version int, title text, abstract text, primary_category text,
                        published_on date, updated_on date, source_url text)
              authors(id bigint, name text, name_normalized text)
              paper_authors(document_id uuid -> documents.id, author_id bigint -> authors.id, position int)
              paper_categories(document_id uuid -> documents.id, category text, is_primary boolean)
            Rules:
            - PostgreSQL dialect, exactly one SELECT statement (WITH is allowed), no semicolons, never SELECT *.
            - Match authors with authors.name_normalized = '<name lowercased, accents and punctuation removed>'.
            - Match categories through paper_categories.category (primary and cross-listed), e.g. 'cs.IR'.
            - Filter dates with documents.published_on.
            Reply with only a JSON object: {"sql": "<query>"}.

            Examples:
            Q: How many cs.CL papers were published in 2025?
            {"sql": "select count(distinct d.id) as n from documents d join paper_categories pc on pc.document_id = d.id where pc.category = 'cs.CL' and d.published_on between '2025-01-01' and '2025-12-31'"}
            Q: List the titles of papers by Jimmy Lin.
            {"sql": "select d.title from documents d join paper_authors pa on pa.document_id = d.id join authors a on a.id = pa.author_id where a.name_normalized = 'jimmy lin' order by d.published_on"}
            Q: Which three authors have the most cs.IR papers?
            {"sql": "select a.name, count(*) as papers from authors a join paper_authors pa on pa.author_id = a.id join paper_categories pc on pc.document_id = pa.document_id where pc.category = 'cs.IR' group by a.name order by papers desc, a.name limit 3"}
            Q: How many papers were published per year?
            {"sql": "select extract(year from published_on)::int as year, count(*) as papers from documents group by 1 order by 1"}
            """.trimIndent()
    }
}
