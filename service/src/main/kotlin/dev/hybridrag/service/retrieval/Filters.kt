package dev.hybridrag.service.retrieval

import dev.hybridrag.service.api.FilterResult
import dev.hybridrag.service.api.FilterSettings
import dev.hybridrag.service.api.FilterValues
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.stereotype.Component
import org.springframework.transaction.support.TransactionTemplate
import java.text.Normalizer
import java.time.LocalDate
import java.util.concurrent.atomic.AtomicReference

/** Contract C4 author-name normalization. */
object AuthorNormalizer {
    private val notAllowed = Regex("[^a-z0-9 -]")
    private val whitespace = Regex("\\s+")

    fun normalize(input: String): String =
        Normalizer
            .normalize(input, Normalizer.Form.NFKD)
            .filterNot { Character.getType(it) == Character.NON_SPACING_MARK.toInt() }
            .lowercase()
            .replace(notAllowed, " ")
            .replace(whitespace, " ")
            .trim()
}

/** Returns which of the candidate normalized names exist in `authors`. */
fun interface AuthorLookup {
    fun existing(candidates: Set<String>): Set<String>
}

@Component
class JdbcAuthorLookup(
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
) : AuthorLookup {
    override fun existing(candidates: Set<String>): Set<String> {
        if (candidates.isEmpty()) return emptySet()
        return jdbc
            .query(
                "select name_normalized from authors where name_normalized = any(:cands)",
                MapSqlParameterSource("cands", textArray(candidates)),
            ) { row, _ -> row.getString(1) }
            .toSet()
    }
}

/** Deterministic, rule-based filter extraction from the question (design D6). */
@Component
class FilterExtractor(
    private val authors: AuthorLookup,
) {
    private val year = "(20\\d{2})"
    private val between = Regex("\\bbetween\\s+$year\\s+and\\s+$year\\b")
    private val inYear = Regex("\\b(?:published\\s+in|in|during)\\s+$year\\b")

    // `YYYY papers`, also with a category code in between as in the C6 example ("2024 cs.IR papers").
    private val yearNoun = Regex("\\b$year\\s+(?:cs\\.[a-z]{2}\\s+)?(?:papers?|work|studies|articles?)\\b")
    private val since = Regex("\\b(?:since|from)\\s+$year\\b")
    private val after = Regex("\\bafter\\s+$year\\b")
    private val before = Regex("\\b(?:before|prior\\s+to)\\s+$year\\b")
    private val categoryCode = Regex("\\bcs\\.(IR|CL|DB|LG|AI|CV|DS|HC|SE|CR|NE|MA)\\b", RegexOption.IGNORE_CASE)
    private val categoryPhrase =
        Regex(
            "\\b(information retrieval|computation and language|computational linguistics|databases?)\\s+(?:papers?|category)\\b",
            RegexOption.IGNORE_CASE,
        )
    private val phraseCategories =
        mapOf(
            "information retrieval" to "cs.IR",
            "computation and language" to "cs.CL",
            "computational linguistics" to "cs.CL",
            "database" to "cs.DB",
            "databases" to "cs.DB",
        )

    // A capitalized name token; an inner apostrophe must be followed by a capital (O'Neil), so "Lin's" yields "Lin".
    private val nameToken = "\\p{Lu}[\\p{L}.\\-]*(?:'\\p{Lu}[\\p{L}.\\-]*)?"
    private val capitalizedRun = Regex("$nameToken(?:\\s+$nameToken)+")

    fun extract(question: String): FilterValues {
        val (from, to) = dates(question.lowercase())
        val categories =
            (
                categoryCode.findAll(question).map { "cs." + it.groupValues[1].uppercase() } +
                    categoryPhrase.findAll(question).map { phraseCategories.getValue(it.groupValues[1].lowercase()) }
            ).distinct().toList()
        return FilterValues(
            categories = categories.ifEmpty { null },
            authors = authors(question).ifEmpty { null },
            publishedFrom = from,
            publishedTo = to,
        )
    }

    private fun dates(text: String): Pair<LocalDate?, LocalDate?> {
        fun first(value: String) = LocalDate.of(value.toInt(), 1, 1)

        fun last(value: String) = LocalDate.of(value.toInt(), 12, 31)
        between.find(text)?.let { return first(it.groupValues[1]) to last(it.groupValues[2]) }
        (inYear.find(text) ?: yearNoun.find(text))?.let { return first(it.groupValues[1]) to last(it.groupValues[1]) }
        val from = since.find(text)?.let { first(it.groupValues[1]) } ?: after.find(text)?.let { first(it.groupValues[1]).plusYears(1) }
        val to = before.find(text)?.let { last(it.groupValues[1]).minusYears(1) }
        return from to to
    }

    /**
     * Candidate spans are runs of capitalized tokens: this covers the D6 triggers (`by`, `authored by`,
     * `co-authored with`, `from`, `<name>'s papers`) and also a bare name such as "What did Jimmy Lin publish …".
     * Every 2–4-token sub-span is normalized (C4) and looked up in one query; only hits are applied, longest first.
     */
    private fun authors(question: String): List<String> {
        val spans =
            capitalizedRun
                .findAll(question)
                .flatMap { run ->
                    val tokens = run.value.split(Regex("\\s+"))
                    (minOf(4, tokens.size) downTo 2).flatMap { size ->
                        (0..tokens.size - size).map { start ->
                            AuthorNormalizer.normalize(tokens.subList(start, start + size).joinToString(" "))
                        }
                    }
                }.filter { it.contains(' ') }
                .toCollection(LinkedHashSet())
        val hits = authors.existing(spans)
        return spans.filter { it in hits }.filterNot { hit -> hits.any { it != hit && " $it ".contains(" $hit ") } }
    }
}

/** Applied filters plus the pushdown decision of design D4 / contract C6. */
data class FilterPlan(
    val applied: FilterValues,
    val extracted: FilterValues,
    val strategy: String,
    val selectivity: Double?,
    val matching: Long?,
    val underfilled: Boolean = false,
) {
    fun response() = FilterResult(applied, extracted, strategy, selectivity, matching, underfilled)
}

@Component
class FilterPlanner(
    private val extractor: FilterExtractor,
    @Qualifier("readerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
) {
    private val total = AtomicReference<Pair<Long, Long>?>(null)

    /** Explicit fields win per field; extraction fills the rest. The estimate runs only when the dense leg needs it. */
    fun plan(
        question: String,
        settings: FilterSettings,
        vectorLeg: Boolean,
    ): FilterPlan {
        val extracted = if (settings.extractFromQuestion) extractor.extract(question) else FilterValues()
        val explicit = settings.explicit ?: FilterValues()
        val applied =
            FilterValues(
                categories = explicit.categories ?: extracted.categories,
                authors = explicit.authors?.map(AuthorNormalizer::normalize) ?: extracted.authors,
                publishedFrom = explicit.publishedFrom ?: extracted.publishedFrom,
                publishedTo = explicit.publishedTo ?: extracted.publishedTo,
            )
        if (applied.isEmpty() || !vectorLeg) return FilterPlan(applied, extracted, "none", null, null)
        val matching = estimate(applied)
        val all = totalChunks()
        val selectivity = if (all == 0L) 0.0 else matching.toDouble() / all
        val strategy =
            when (settings.strategy) {
                "pre", "post" -> settings.strategy
                else -> if (selectivity < settings.selectivityThreshold) "pre" else "post"
            }
        return FilterPlan(applied, extracted, strategy, selectivity, matching)
    }

    private fun estimate(applied: FilterValues): Long =
        transaction.execute {
            setLocal(jdbc, "statement_timeout", "500")
            jdbc.queryForObject(
                "select count(*) from chunks c join documents d on d.id = c.document_id where true${FilterSql.fragment(applied)}",
                FilterSql.parameters(applied),
                Long::class.java,
            )
        } ?: 0L

    /** `select count(*) from chunks`, cached for 60 s. */
    private fun totalChunks(): Long {
        val now = System.currentTimeMillis()
        total.get()?.takeIf { now - it.first < 60_000 }?.let { return it.second }
        val count =
            transaction.execute {
                setLocal(jdbc, "statement_timeout", "500")
                jdbc.jdbcTemplate.queryForObject("select count(*) from chunks", Long::class.java)
            } ?: 0L
        total.set(now to count)
        return count
    }
}

/** The filter fragment `F` of design D4: only lines for set filters, every value bound. */
object FilterSql {
    fun fragment(filters: FilterValues?): String =
        buildString {
            if (filters == null) return@buildString
            if (!filters.categories.isNullOrEmpty()) {
                append("\n  and exists (select 1 from paper_categories pc where pc.document_id = d.id and pc.category = any(:categories))")
            }
            if (!filters.authors.isNullOrEmpty()) {
                append("\n  and exists (select 1 from paper_authors pa join authors a on a.id = pa.author_id")
                append(" where pa.document_id = d.id and a.name_normalized = any(:authors))")
            }
            if (filters.publishedFrom != null) append("\n  and d.published_on >= :published_from")
            if (filters.publishedTo != null) append("\n  and d.published_on <= :published_to")
        }

    fun parameters(filters: FilterValues?): MapSqlParameterSource =
        MapSqlParameterSource().apply {
            if (filters == null) return@apply
            filters.categories?.takeIf { it.isNotEmpty() }?.let { addValue("categories", textArray(it)) }
            filters.authors?.takeIf { it.isNotEmpty() }?.let { addValue("authors", textArray(it)) }
            filters.publishedFrom?.let { addValue("published_from", it) }
            filters.publishedTo?.let { addValue("published_to", it) }
        }
}
