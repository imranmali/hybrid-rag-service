package dev.hybridrag.service.retrieval

import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class FusionTest {
    private val fusion = RrfFusion()

    @Test
    fun `a chunk appearing in both legs outranks single leg rank one`() {
        val result =
            fusion.fuse(
                listOf(Candidate(1, vectorRank = 2), Candidate(2, vectorRank = 1)),
                listOf(Candidate(1, bm25Rank = 2)),
                60,
                50,
            )
        assertEquals(1, result.first().id)
        assertEquals(2.0 / 62, result.first().fused)
    }

    @Test
    fun `single leg preserves scores without fusion`() {
        val candidate = Candidate(1, vector = 0.9, vectorRank = 1)
        val result = fusion.fuse(listOf(candidate), emptyList(), 60, 50)
        assertEquals(1, result.single().id)
        assertNull(result.single().fused)
    }
}
