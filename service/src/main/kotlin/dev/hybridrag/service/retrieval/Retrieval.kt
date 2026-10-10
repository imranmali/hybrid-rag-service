package dev.hybridrag.service.retrieval

import com.pgvector.PGvector
import dev.hybridrag.service.RagProperties
import dev.hybridrag.service.api.ChunkResponse
import dev.hybridrag.service.api.ChunkScores
import dev.hybridrag.service.api.ExplicitFilters
import dev.hybridrag.service.api.FilterResult
import dev.hybridrag.service.api.FilterSettings
import dev.hybridrag.service.api.Pipeline
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.stereotype.Component
import org.springframework.transaction.support.TransactionTemplate
import java.text.Normalizer
import java.time.LocalDate
import java.util.concurrent.atomic.AtomicReference

data class Candidate(
    val id: Long,
    var vector: Double? = null,
    var vectorRank: Int? = null,
    var bm25: Double? = null,
    var bm25Rank: Int? = null,
    var fused: Double? = null,
    var rerank: Double? = null,
)

data class AppliedFilters(
    val explicit: ExplicitFilters,
    val extracted: ExplicitFilters,
    val strategy: String,
    val selectivity: Double?,
    val matching: Long?,
    val underfilled: Boolean = false,
) {
    fun response(): FilterResult = FilterResult(explicit, extracted, strategy, selectivity, matching, underfilled)

    fun hasConstraints(): Boolean =
        !explicit.categories.isNullOrEmpty() ||
            !explicit.authors.isNullOrEmpty() ||
            explicit.publishedFrom != null ||
            explicit.publishedTo != null
}

object AuthorNormalizer {
    fun normalize(input: String): String =
        Normalizer
            .normalize(input, Normalizer.Form.NFKD)
            .filterNot { Character.getType(it) == Character.NON_SPACING_MARK.toInt() }
            .lowercase()
            .replace(Regex("[^a-z0-9 -]"), " ")
            .replace(Regex("\\s+"), " ")
            .trim()
}

@Component
class FilterExtractor(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
) {
    private val categoryPhrases =
        mapOf(
            "information retrieval" to "cs.IR",
            "computation and language" to "cs.CL",
            "computational linguistics" to "cs.CL",
            "database" to "cs.DB",
            "databases" to "cs.DB",
        )

    fun extract(question: String): ExplicitFilters {
        val lower = question.lowercase()
        val dates = dates(lower)
        val categories =
            buildSet {
                Regex("\\bcs\\.(IR|CL|DB|LG|AI|CV|DS|HC|SE|CR|NE|MA)\\b", RegexOption.IGNORE_CASE)
                    .findAll(question)
                    .forEach { add("cs.${it.groupValues[1].uppercase()}") }
                categoryPhrases.filterKeys(lower::contains).values.forEach(::add)
            }.toList().ifEmpty { null }
        val candidates =
            Regex(
                "(?:authored by|co-authored with|by|from)\\s+([A-Z][\\p{L}'\\-.]+(?:\\s+[A-Z][\\p{L}'\\-.]+){1,3})|" +
                    "([A-Z][\\p{L}'\\-.]+(?:\\s+[A-Z][\\p{L}'\\-.]+){1,3})(?='s\\s+(?:paper|papers|work))",
            ).findAll(question)
                .flatMap { match ->
                    val name = match.groupValues.drop(1).firstOrNull { it.isNotBlank() } ?: ""
                    val parts = AuthorNormalizer.normalize(name).split(" ")
                    (parts.size downTo 2).map { parts.take(it).joinToString(" ") }
                }.toSet()
        val authors = verifiedAuthors(candidates).ifEmpty { null }
        return ExplicitFilters(categories, authors, dates.first, dates.second)
    }

    private fun verifiedAuthors(candidates: Set<String>): List<String> {
        if (candidates.isEmpty()) return emptyList()
        return jdbc.query(
            "select name_normalized from authors where name_normalized = any(:names)",
            mapOf("names" to candidates.toTypedArray()),
        ) { row, _ -> row.getString(1) }
    }

    private fun dates(question: String): Pair<LocalDate?, LocalDate?> {
        fun year(value: String) = value.toInt()
        Regex("between\\s+(20\\d{2})\\s+and\\s+(20\\d{2})").find(question)?.let {
            return LocalDate.of(year(it.groupValues[1]), 1, 1) to LocalDate.of(year(it.groupValues[2]), 12, 31)
        }
        Regex("(?:in|during|published in)\\s+(20\\d{2})").find(question)?.let {
            return LocalDate.of(year(it.groupValues[1]), 1, 1) to LocalDate.of(year(it.groupValues[1]), 12, 31)
        }
        Regex("(?:since|from)\\s+(20\\d{2})").find(question)?.let { return LocalDate.of(year(it.groupValues[1]), 1, 1) to null }
        Regex("after\\s+(20\\d{2})").find(question)?.let { return LocalDate.of(year(it.groupValues[1]) + 1, 1, 1) to null }
        Regex("(?:before|prior to)\\s+(20\\d{2})").find(question)?.let { return null to LocalDate.of(year(it.groupValues[1]) - 1, 12, 31) }
        Regex("\\b(20\\d{2})\\s+(?:papers?|work|studies|articles?)").find(question)?.let {
            return LocalDate.of(year(it.groupValues[1]), 1, 1) to LocalDate.of(year(it.groupValues[1]), 12, 31)
        }
        return null to null
    }
}

@Component
class FilterPlanner(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
) {
    private val cachedTotal = AtomicReference(0L to 0L)

    fun plan(
        question: String,
        settings: FilterSettings,
        extractor: FilterExtractor,
    ): AppliedFilters? {
        if (!settings.enabled) return null
        val extracted = if (settings.extractFromQuestion) extractor.extract(question) else ExplicitFilters()
        val explicit = settings.explicit ?: ExplicitFilters()
        val applied =
            ExplicitFilters(
                categories = explicit.categories ?: extracted.categories,
                authors = explicit.authors?.map(AuthorNormalizer::normalize) ?: extracted.authors,
                publishedFrom = explicit.publishedFrom ?: extracted.publishedFrom,
                publishedTo = explicit.publishedTo ?: extracted.publishedTo,
            )
        if (applied.categories.isNullOrEmpty() &&
            applied.authors.isNullOrEmpty() &&
            applied.publishedFrom == null &&
            applied.publishedTo == null
        ) {
            return AppliedFilters(applied, extracted, "none", null, null)
        }
        val count =
            transaction.execute<Long> {
                jdbc.queryForObject(
                    "select count(*) from chunks c join documents d on d.id = c.document_id where true ${filterSql(applied)}",
                    params(applied),
                    Long::class.java,
                ) ?: 0
            } ?: 0
        val total = totalChunks()
        val selectivity = if (total == 0L) 0.0 else count.toDouble() / total
        val strategy =
            when (settings.strategy) {
                "pre", "post" -> settings.strategy
                else -> if (selectivity < settings.selectivityThreshold) "pre" else "post"
            }
        return AppliedFilters(applied, extracted, strategy, selectivity, count)
    }

    private fun totalChunks(): Long {
        val now = System.currentTimeMillis()
        val current = cachedTotal.get()
        if (now - current.first < 60_000) return current.second
        val total = jdbc.jdbcTemplate.queryForObject("select count(*) from chunks", Long::class.java) ?: 0
        cachedTotal.set(now to total)
        return total
    }
}

@Component
class VectorRetriever(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
    private val properties: RagProperties,
) {
    fun retrieve(
        vector: List<Float>,
        pipeline: Pipeline,
        filters: AppliedFilters?,
    ): Pair<List<Candidate>, Boolean> {
        val contextual = pipeline.contextual
        val column = if (contextual) "embedding_ctx" else "embedding"
        val query =
            when (filters?.strategy) {
                "pre" ->
                    """
                    with filtered as materialized (
                      select c.id, c.$column as emb from chunks c join documents d on d.id = c.document_id
                      where true ${filterSql(filters.explicit)}
                    ) select id, 1 - (emb <=> :vector) as sim from filtered
                    order by emb <=> :vector, id limit :limit
                    """.trimIndent()
                "post" ->
                    """
                    with ann as materialized (
                      select c.id, c.document_id, c.$column <=> :vector as dist from chunks c
                      order by c.$column <=> :vector limit :fetch
                    ) select ann.id, 1 - ann.dist as sim from ann join documents d on d.id = ann.document_id
                    where true ${filterSql(filters.explicit)} order by ann.dist, ann.id limit :limit
                    """.trimIndent()
                else ->
                    "select c.id, 1 - (c.$column <=> :vector) as sim from chunks c " +
                        "order by c.$column <=> :vector" +
                        (if (properties.retrieval.exactVectorSearch) ", c.id limit :limit" else " limit :limit")
            }
        val values =
            params(filters?.explicit ?: ExplicitFilters())
                .addValue("vector", PGvector(vector.toFloatArray()))
                .addValue("limit", pipeline.retrieval.vectorK)
                .addValue("fetch", pipeline.retrieval.vectorK * pipeline.filters.overfetchFactor)
        val rows =
            transaction.execute<List<Candidate>> {
                jdbc.jdbcTemplate.execute("select set_config('statement_timeout', '1500', true)")
                jdbc.query(query, values) { row, index ->
                    Candidate(row.getLong("id"), vector = row.getDouble("sim"), vectorRank = index + 1)
                }
            } ?: emptyList()
        return rows to (filters?.strategy == "post" && rows.size < pipeline.retrieval.vectorK)
    }
}

@Component
class LexicalRetriever(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
) {
    fun retrieve(
        question: String,
        pipeline: Pipeline,
        filters: AppliedFilters?,
    ): List<Candidate> {
        val column = if (pipeline.contextual) "tsv_ctx" else "tsv"
        val sql =
            """
            with q as (select replace(plainto_tsquery('english', :question)::text, '&', '|')::tsquery as tsq)
            select c.id, ts_rank_cd(c.$column, q.tsq, 32) as score
            from q cross join chunks c join documents d on d.id = c.document_id
            where numnode(q.tsq) > 0 and c.$column @@ q.tsq ${filterSql(filters?.explicit ?: ExplicitFilters())}
            order by score desc, c.id limit :limit
            """.trimIndent()
        val parameters =
            params(filters?.explicit ?: ExplicitFilters())
                .addValue("question", question)
                .addValue("limit", pipeline.retrieval.bm25K)
        return transaction.execute<List<Candidate>> {
            jdbc.jdbcTemplate.execute("select set_config('statement_timeout', '1500', true)")
            jdbc.query(sql, parameters) { row, index ->
                Candidate(row.getLong("id"), bm25 = row.getDouble("score"), bm25Rank = index + 1)
            }
        } ?: emptyList()
    }
}

@Component
class ChunkHydrator(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
) {
    fun hydrate(candidates: List<Candidate>): List<ChunkResponse> {
        if (candidates.isEmpty()) return emptyList()
        val rows =
            jdbc
                .query(
                    """
                    select c.id, d.external_id, c.ordinal, d.title, c.section_path, d.published_on, d.primary_category,
                           c.content, c.context_header
                    from chunks c join documents d on d.id = c.document_id where c.id = any(:ids)
                    """.trimIndent(),
                    mapOf("ids" to candidates.map(Candidate::id).toTypedArray()),
                ) { result, _ ->
                    result.getLong("id") to
                        ChunkResponse(
                            rank = 0,
                            chunkId = result.getLong("id"),
                            externalId = result.getString("external_id"),
                            ordinal = result.getInt("ordinal"),
                            title = result.getString("title"),
                            sectionPath = result.getString("section_path"),
                            publishedOn = result.getObject("published_on", LocalDate::class.java),
                            primaryCategory = result.getString("primary_category"),
                            content = result.getString("content"),
                            contextHeader = result.getString("context_header"),
                            scores = ChunkScores(),
                        )
                }.toMap()
        return candidates.mapIndexedNotNull { index, candidate ->
            rows[candidate.id]?.copy(
                rank = index + 1,
                scores =
                    ChunkScores(
                        candidate.vector,
                        candidate.vectorRank,
                        candidate.bm25,
                        candidate.bm25Rank,
                        candidate.fused,
                        candidate.rerank,
                    ),
            )
        }
    }
}

@Component
class RrfFusion {
    fun fuse(
        vector: List<Candidate>,
        lexical: List<Candidate>,
        k: Int,
        returnK: Int,
    ): List<Candidate> {
        if (vector.isEmpty()) return lexical.take(returnK)
        if (lexical.isEmpty()) return vector.take(returnK)
        val merged = mutableMapOf<Long, Candidate>()

        fun merge(source: List<Candidate>) =
            source.forEach {
                val target = merged.getOrPut(it.id) { Candidate(it.id) }
                target.vector = it.vector ?: target.vector
                target.vectorRank = it.vectorRank ?: target.vectorRank
                target.bm25 = it.bm25 ?: target.bm25
                target.bm25Rank = it.bm25Rank ?: target.bm25Rank
            }
        merge(vector)
        merge(lexical)
        return merged.values
            .map { candidate ->
                candidate.fused = listOfNotNull(candidate.vectorRank, candidate.bm25Rank).sumOf { 1.0 / (k + it) }
                candidate
            }.sortedWith(compareByDescending<Candidate> { it.fused }.thenBy { it.id })
            .take(returnK)
    }
}

private fun filterSql(filters: ExplicitFilters): String =
    buildString {
        if (!filters.categories.isNullOrEmpty()) {
            append(" and exists (select 1 from paper_categories pc where pc.document_id = d.id and pc.category = any(:categories))")
        }
        if (!filters.authors.isNullOrEmpty()) {
            append(
                " and exists (select 1 from paper_authors pa join authors a on a.id = pa.author_id " +
                    "where pa.document_id = d.id and a.name_normalized = any(:authors))",
            )
        }
        if (filters.publishedFrom != null) append(" and d.published_on >= :publishedFrom")
        if (filters.publishedTo != null) append(" and d.published_on <= :publishedTo")
    }

private fun params(filters: ExplicitFilters): MapSqlParameterSource =
    MapSqlParameterSource().apply {
        filters.categories?.let { addValue("categories", it.toTypedArray()) }
        filters.authors?.let { addValue("authors", it.toTypedArray()) }
        filters.publishedFrom?.let { addValue("publishedFrom", it) }
        filters.publishedTo?.let { addValue("publishedTo", it) }
    }
