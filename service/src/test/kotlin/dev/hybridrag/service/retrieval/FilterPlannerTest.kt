package dev.hybridrag.service.retrieval

import dev.hybridrag.service.api.FilterSettings
import dev.hybridrag.service.api.FilterValues
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.WireMockFixture
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import java.time.LocalDate
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Task 3.4: selectivity estimate and pushdown strategy against the seeded database. */
class FilterPlannerTest : IntegrationTest() {
    @Autowired
    lateinit var planner: FilterPlanner

    @Autowired
    lateinit var vectorRetriever: VectorRetriever

    private val oneCategoryYear =
        FilterValues(categories = listOf("cs.DB"), publishedFrom = LocalDate.of(2024, 1, 1), publishedTo = LocalDate.of(2024, 12, 31))

    @Test
    fun `a filter matching one category-year (about 17 percent) uses pre and equals exact filtered search`() {
        val plan = planner.plan("x", FilterSettings(explicit = oneCategoryYear), vectorLeg = true)
        assertEquals("pre", plan.strategy)
        assertEquals(20L, plan.matching)
        assertEquals(20.0 / Fixture.CHUNKS, plan.selectivity!!, 1e-9)
        val query = RetrieverTest.queryVector(11)
        val result = vectorRetriever.retrieve(query, contextual = true, vectorK = 10, overfetchFactor = 5, plan = plan)
        val doc4 = (0 until Fixture.CHUNKS_PER_DOCUMENT).map { Fixture.chunkId(4, it) }
        assertEquals(RetrieverTest.bruteForce(query, doc4, contextual = true).take(10), result.candidates.map { it.id })
        assertEquals(false, result.underfilled)
    }

    @Test
    fun `a filter matching 50 percent or more uses post`() {
        val plan = planner.plan("x", FilterSettings(explicit = FilterValues(publishedTo = LocalDate.of(2024, 12, 31))), vectorLeg = true)
        assertEquals(60L, plan.matching)
        assertEquals("post", plan.strategy)
        val result =
            vectorRetriever.retrieve(
                RetrieverTest.queryVector(12),
                contextual = true,
                vectorK = 10,
                overfetchFactor = 5,
                plan = plan,
            )
        assertEquals(10, result.candidates.size)
        assertTrue(result.candidates.all { Fixture.docOf(it.id).year == 2024 })
    }

    @Test
    fun `cross-listed categories match`() {
        assertEquals(
            20L,
            planner.plan("x", FilterSettings(explicit = FilterValues(categories = listOf("cs.LG"))), vectorLeg = true).matching,
        )
    }

    @Test
    fun `no filter or no vector leg means strategy none with null estimate`() {
        val none = planner.plan("What is ColBERT?", FilterSettings(), vectorLeg = true)
        assertEquals("none", none.strategy)
        assertNull(none.selectivity)
        assertNull(none.matching)
        val lexicalOnly = planner.plan("x", FilterSettings(explicit = oneCategoryYear), vectorLeg = false)
        assertEquals("none", lexicalOnly.strategy)
        assertEquals(oneCategoryYear, lexicalOnly.applied)
    }

    @Test
    fun `explicit fields override extracted ones per field and authors are normalized`() {
        val plan =
            planner.plan(
                "What did Jimmy Lin publish in 2024?",
                FilterSettings(explicit = FilterValues(authors = listOf("  Alice   Smith"), publishedFrom = LocalDate.of(2025, 1, 1))),
                vectorLeg = true,
            )
        assertEquals(
            FilterValues(authors = listOf("jimmy lin"), publishedFrom = LocalDate.of(2024, 1, 1), publishedTo = LocalDate.of(2024, 12, 31)),
            plan.extracted,
        )
        assertEquals(
            FilterValues(
                authors = listOf("alice smith"),
                publishedFrom = LocalDate.of(2025, 1, 1),
                publishedTo = LocalDate.of(2024, 12, 31),
            ),
            plan.applied,
        )
    }

    @Test
    fun `forced post on a one-document filter is underfilled in the response`() {
        WireMockFixture.stubEmbed(Fixture.vector(Fixture.chunkId(4, 0), true))
        val result =
            query(
                "anything",
                """{"retrieval": {"bm25": false}, "filters": {"strategy": "post", "explicit": {"categories": ["cs.DB"],
                    "published_from": "2024-01-01", "published_to": "2024-12-31"}}, $MODELS_OFF}""",
            )
        assertEquals(200, result.status)
        val filters = result.json["filters"]
        assertEquals("post", filters["strategy"].asText())
        assertEquals(true, filters["underfilled"].asBoolean())
        assertEquals(20, result.json["chunks"].size())
    }

    @Test
    fun `year and author extracted from the question restrict every leg`() {
        WireMockFixture.stubEmbed(Fixture.vector(Fixture.chunkId(1, 0), true))
        val result = query("What did Jimmy Lin publish on rerankers in 2024?", """{$MODELS_OFF}""")
        assertEquals(200, result.status)
        val extracted = result.json["filters"]["extracted"]
        // Only the set filter fields appear (contract C6 example), nothing else.
        assertEquals(setOf("authors", "published_from", "published_to"), extracted.fieldNames().asSequence().toSet())
        assertEquals(
            setOf("applied", "extracted", "strategy", "estimated_selectivity", "matching_chunks", "underfilled"),
            result.json["filters"]
                .fieldNames()
                .asSequence()
                .toSet(),
        )
        assertEquals("jimmy lin", extracted["authors"][0].asText())
        assertEquals("2024-01-01", extracted["published_from"].asText())
        assertEquals("2024-12-31", extracted["published_to"].asText())
        assertTrue(result.json["chunks"].size() > 0)
        result.json["chunks"].forEach { chunk ->
            val doc = Fixture.docOf(chunk["chunk_id"].asLong())
            assertTrue(doc.year == 2024 && "Jimmy Lin" in doc.authors, "chunk ${chunk["chunk_id"]} from $doc")
        }
    }

    @Test
    fun `unknown author is ignored`() {
        WireMockFixture.stubEmbed(Fixture.vector(1, true))
        val result = query("Papers by Ada Nobody on reranking", """{$MODELS_OFF}""")
        assertTrue(result.json["filters"]["applied"]["authors"] == null)
        assertEquals("none", result.json["filters"]["strategy"].asText())
    }

    @Test
    fun `filters disabled gives null filters and no date restriction`() {
        WireMockFixture.stubEmbed(Fixture.vector(Fixture.chunkId(1, 0), true))
        val result = query("What was published in 2024?", """{"filters": {"enabled": false}, $MODELS_OFF}""")
        assertTrue(result.json["filters"].isNull)
        assertTrue(result.json["chunks"].any { Fixture.docOf(it["chunk_id"].asLong()).year == 2025 })
        assertTrue(
            "filter.estimate" !in
                result.json["timings_ms"]
                    .fieldNames()
                    .asSequence()
                    .toList(),
        )
    }

    companion object {
        const val MODELS_OFF = """"rerank": {"enabled": false}, "routing": {"enabled": false}, "generation": {"enabled": false}"""
    }
}
