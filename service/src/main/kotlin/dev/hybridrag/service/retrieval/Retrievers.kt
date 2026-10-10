package dev.hybridrag.service.retrieval

import com.pgvector.PGvector
import dev.hybridrag.service.api.ChunkResponse
import dev.hybridrag.service.api.ChunkScores
import dev.hybridrag.service.config.RagProperties
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.jdbc.core.support.AbstractSqlTypeValue
import org.springframework.stereotype.Component
import org.springframework.transaction.support.TransactionTemplate
import java.sql.Connection
import java.time.LocalDate

/** A Postgres array parameter (`= any(:x)`); a plain collection would be expanded into an IN list by Spring. */
internal fun textArray(values: Collection<String>) = sqlArray("text", values.toTypedArray())

internal fun bigintArray(values: Collection<Long>) = sqlArray("bigint", values.toTypedArray())

private fun sqlArray(
    type: String,
    values: Array<*>,
) = object : AbstractSqlTypeValue() {
    override fun createTypeValue(
        connection: Connection,
        sqlType: Int,
        typeName: String?,
    ) = connection.createArrayOf(type, values)
}

/** `set_config(name, value, true)`: a transaction-local setting. */
internal fun setLocal(
    jdbc: NamedParameterJdbcTemplate,
    name: String,
    value: String,
) {
    jdbc.jdbcTemplate.queryForObject("select set_config(?, ?, true)", String::class.java, name, value)
}

data class VectorResult(
    val candidates: List<Candidate>,
    val underfilled: Boolean,
)

/** Dense leg (design D4): plain ANN, pre-filter exact, post-filter over-fetch, and exact mode (contract C9). */
@Component
class VectorRetriever(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
    private val properties: RagProperties,
) {
    fun retrieve(
        embedding: FloatArray,
        contextual: Boolean,
        vectorK: Int,
        overfetchFactor: Int,
        plan: FilterPlan?,
    ): VectorResult {
        // Column chosen from a fixed pair, never interpolated from input.
        val col = if (contextual) "embedding_ctx" else "embedding"
        val exact = properties.retrieval.exactVectorSearch
        val tie = if (exact) ", c.id" else ""
        val strategy = plan?.strategy ?: "none"
        val fetch = vectorK * overfetchFactor
        val sql =
            when (strategy) {
                "pre" ->
                    """
                    with filtered as materialized (
                      select c.id, c.$col as emb from chunks c join documents d on d.id = c.document_id
                      where true${FilterSql.fragment(plan?.applied)}
                    )
                    select id, 1 - (emb <=> :q) as sim from filtered order by emb <=> :q, id limit :vector_k
                    """.trimIndent()
                "post" ->
                    """
                    with ann as materialized (
                      select c.id, c.document_id, c.$col <=> :q as dist from chunks c order by c.$col <=> :q$tie limit :fetch
                    )
                    select ann.id, 1 - ann.dist as sim from ann join documents d on d.id = ann.document_id
                    where true${FilterSql.fragment(plan?.applied)}
                    order by ann.dist, ann.id limit :vector_k
                    """.trimIndent()
                else -> "select c.id, 1 - (c.$col <=> :q) as sim from chunks c order by c.$col <=> :q$tie limit :vector_k"
            }
        val parameters =
            (if (strategy == "pre" || strategy == "post") FilterSql.parameters(plan?.applied) else MapSqlParameterSource())
                .addValue("q", PGvector(embedding))
                .addValue("vector_k", vectorK)
                .addValue("fetch", fetch)
        val rows =
            transaction
                .execute {
                    setLocal(jdbc, "statement_timeout", "1500")
                    when (strategy) {
                        // ef_search must be ≥ vector_k or HNSW silently caps the result (contract C11: 100 for k = 50).
                        "pre" -> Unit
                        "post" -> {
                            setLocal(jdbc, "hnsw.ef_search", fetch.toString())
                            setLocal(jdbc, "hnsw.iterative_scan", "off")
                        }
                        else -> setLocal(jdbc, "hnsw.ef_search", maxOf(100, vectorK).toString())
                    }
                    if (exact) {
                        setLocal(jdbc, "enable_indexscan", "off")
                        setLocal(jdbc, "enable_bitmapscan", "off")
                    }
                    jdbc.query(sql, parameters) { row, _ -> row.getLong("id") to row.getDouble("sim") }
                }.orEmpty()
        // ANN orders by distance only (so the HNSW index stays usable); ties are re-sorted by id here.
        val candidates =
            rows
                .sortedWith(compareByDescending<Pair<Long, Double>> { it.second }.thenBy { it.first })
                .mapIndexed { index, (id, sim) -> Candidate(id, vector = sim, vectorRank = index + 1) }
        return VectorResult(candidates, strategy == "post" && candidates.size < vectorK)
    }
}

/** Lexical leg (design D4): OR-semantics tsquery, `ts_rank_cd(…, 32)`, ties by lower id. */
@Component
class LexicalRetriever(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
) {
    fun retrieve(
        question: String,
        contextual: Boolean,
        bm25K: Int,
        plan: FilterPlan?,
    ): List<Candidate> {
        val tsv = if (contextual) "tsv_ctx" else "tsv"
        // plainto_tsquery ANDs the stemmed terms; rewriting & → | gives OR semantics without re-stemming.
        val sql =
            """
            with q as (select replace(plainto_tsquery('english', :question)::text, '&', '|')::tsquery as tsq)
            select c.id, ts_rank_cd(c.$tsv, q.tsq, 32) as score
            from q cross join chunks c join documents d on d.id = c.document_id
            where numnode(q.tsq) > 0 and c.$tsv @@ q.tsq${FilterSql.fragment(plan?.applied)}
            order by score desc, c.id
            limit :bm25_k
            """.trimIndent()
        val parameters = FilterSql.parameters(plan?.applied).addValue("question", question).addValue("bm25_k", bm25K)
        return transaction
            .execute {
                setLocal(jdbc, "statement_timeout", "1500")
                jdbc.query(
                    sql,
                    parameters,
                ) { row, index -> Candidate(row.getLong("id"), bm25 = row.getDouble("score"), bm25Rank = index + 1) }
            }.orEmpty()
    }
}

/** Loads chunk and document fields for the final ids in one query, preserving the given order. */
@Component
class ChunkHydrator(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
) {
    fun hydrate(candidates: List<Candidate>): List<ChunkResponse> {
        if (candidates.isEmpty()) return emptyList()
        val sql =
            """
            select c.id, d.external_id, c.ordinal, d.title, c.section_path, d.published_on, d.primary_category,
                   c.content, c.context_header
            from chunks c join documents d on d.id = c.document_id
            where c.id = any(:ids)
            """.trimIndent()
        val rows =
            transaction
                .execute {
                    setLocal(jdbc, "statement_timeout", "1500")
                    jdbc.query(sql, MapSqlParameterSource("ids", bigintArray(candidates.map { it.id }))) { row, _ ->
                        ChunkResponse(
                            rank = 0,
                            chunkId = row.getLong("id"),
                            externalId = row.getString("external_id"),
                            ordinal = row.getInt("ordinal"),
                            title = row.getString("title"),
                            sectionPath = row.getString("section_path"),
                            publishedOn = row.getObject("published_on", LocalDate::class.java),
                            primaryCategory = row.getString("primary_category"),
                            content = row.getString("content"),
                            contextHeader = row.getString("context_header"),
                            scores = ChunkScores(),
                        )
                    }
                }.orEmpty()
        return reorder(rows, candidates)
    }

    /** Re-applies the (possibly re-ordered) candidate scores to already hydrated chunks without a second query. */
    fun reorder(
        hydrated: List<ChunkResponse>,
        candidates: List<Candidate>,
    ): List<ChunkResponse> {
        val byId = hydrated.associateBy { it.chunkId }
        return candidates.mapNotNull { candidate -> byId[candidate.id]?.let { candidate to it } }.mapIndexed { index, (c, chunk) ->
            chunk.copy(rank = index + 1, scores = ChunkScores(c.vector, c.vectorRank, c.bm25, c.bm25Rank, c.fused, c.rerank))
        }
    }
}
