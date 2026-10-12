package dev.hybridrag.service.retrieval

import dev.hybridrag.service.api.FilterValues
import org.junit.jupiter.api.Test
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.Arguments
import org.junit.jupiter.params.provider.MethodSource
import java.time.LocalDate
import kotlin.test.assertEquals

/** Task 3.4: table tests for deterministic extraction (design D6) and the contract C4 normalization vectors. */
class FilterExtractorTest {
    private val known = setOf("jimmy lin", "jose perez-gomez", "alice smith", "sean o neil", "j r r tolkien", "zhang wei")
    private val lookups = mutableListOf<Set<String>>()
    private val extractor =
        FilterExtractor { candidates ->
            lookups += candidates
            candidates.intersect(known)
        }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    fun `extracts filters`(
        question: String,
        expected: FilterValues,
    ) {
        assertEquals(expected, extractor.extract(question))
    }

    @Test
    fun `author candidates are looked up in a single query`() {
        extractor.extract("Papers by Jimmy Lin and Alice Smith on BM25")
        assertEquals(1, lookups.size)
    }

    @Test
    fun `no lookup without capitalized multi-token spans`() {
        lookups.clear()
        extractor.extract("what is dense retrieval?")
        assertEquals(emptySet(), lookups.single())
    }

    @ParameterizedTest(name = "{0} -> {1}")
    @MethodSource("normalization")
    fun `author normalization vectors (contract C4)`(
        input: String,
        expected: String,
    ) {
        assertEquals(expected, AuthorNormalizer.normalize(input))
    }

    companion object {
        private fun d(text: String) = LocalDate.parse(text)

        private fun year(y: Int) = FilterValues(publishedFrom = d("$y-01-01"), publishedTo = d("$y-12-31"))

        @JvmStatic
        fun cases(): List<Arguments> =
            listOf(
                // years
                Arguments.of("Which rerankers were proposed in 2024?", year(2024)),
                Arguments.of("Papers published in 2023 about BM25", year(2023)),
                Arguments.of("What changed during 2025 for dense retrieval?", year(2025)),
                Arguments.of("Dense retrieval work since 2023", FilterValues(publishedFrom = d("2023-01-01"))),
                Arguments.of("Results from 2024 onwards", FilterValues(publishedFrom = d("2024-01-01"))),
                Arguments.of("Approaches after 2023", FilterValues(publishedFrom = d("2024-01-01"))),
                Arguments.of("Methods before 2025", FilterValues(publishedTo = d("2024-12-31"))),
                Arguments.of("Methods prior to 2024", FilterValues(publishedTo = d("2023-12-31"))),
                Arguments.of("Studies between 2023 and 2024", FilterValues(publishedFrom = d("2023-01-01"), publishedTo = d("2024-12-31"))),
                Arguments.of("Summarize the 2025 papers on agents", year(2025)),
                Arguments.of("What do 2024 studies say about chunking?", year(2024)),
                Arguments.of(
                    "Recent work since 2023 but before 2025",
                    FilterValues(publishedFrom = d("2023-01-01"), publishedTo = d("2024-12-31")),
                ),
                Arguments.of("What is ColBERT with 1999 baselines?", FilterValues()),
                Arguments.of("Does BM25 use 2048 tokens?", FilterValues()),
                // categories
                Arguments.of("Which cs.IR papers evaluate rerankers?", FilterValues(categories = listOf("cs.IR"))),
                Arguments.of("Compare CS.cl and cs.db approaches", FilterValues(categories = listOf("cs.CL", "cs.DB"))),
                Arguments.of("information retrieval papers on BM25", FilterValues(categories = listOf("cs.IR"))),
                Arguments.of("computational linguistics papers about parsing", FilterValues(categories = listOf("cs.CL"))),
                Arguments.of("Computation and Language category trends", FilterValues(categories = listOf("cs.CL"))),
                Arguments.of("database papers on learned indexes", FilterValues(categories = listOf("cs.DB"))),
                Arguments.of("What is a database?", FilterValues()),
                // authors
                Arguments.of("What did Jimmy Lin publish on rerankers in 2024?", year(2024).copy(authors = listOf("jimmy lin"))),
                Arguments.of("Summarize Jimmy Lin's papers", FilterValues(authors = listOf("jimmy lin"))),
                Arguments.of("Papers authored by José Pérez-Gómez", FilterValues(authors = listOf("jose perez-gomez"))),
                Arguments.of("Work co-authored with Alice Smith on BM25", FilterValues(authors = listOf("alice smith"))),
                Arguments.of("Retrieval papers from Seán O'Neil", FilterValues(authors = listOf("sean o neil"))),
                Arguments.of("Anything by J. R. R. Tolkien?", FilterValues(authors = listOf("j r r tolkien"))),
                Arguments.of("Papers by ZHANG Wei", FilterValues(authors = listOf("zhang wei"))),
                Arguments.of("Papers by Ada Nobody on BM25", FilterValues()),
                Arguments.of("Did Jimmy Lin Alice Smith collaborate?", FilterValues(authors = listOf("jimmy lin", "alice smith"))),
                Arguments.of("papers by jimmy lin", FilterValues()),
                // combined
                Arguments.of(
                    "Which rerankers did Jimmy Lin's 2024 cs.IR papers evaluate?",
                    FilterValues(listOf("cs.IR"), listOf("jimmy lin"), d("2024-01-01"), d("2024-12-31")),
                ),
                Arguments.of(
                    "information retrieval papers by Alice Smith since 2023",
                    FilterValues(listOf("cs.IR"), listOf("alice smith"), d("2023-01-01"), null),
                ),
            )

        @JvmStatic
        fun normalization(): List<Arguments> =
            listOf(
                Arguments.of("José  Pérez-Gómez", "jose perez-gomez"),
                Arguments.of("J. R. R. Tolkien", "j r r tolkien"),
                Arguments.of("Seán O'Neil", "sean o neil"),
                Arguments.of("ZHANG Wei", "zhang wei"),
                Arguments.of("  Jimmy   Lin ", "jimmy lin"),
            )
    }
}
