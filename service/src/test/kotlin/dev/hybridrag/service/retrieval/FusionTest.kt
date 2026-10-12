package dev.hybridrag.service.retrieval

import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Task 3.5: RRF and the rerank merge of design D5. */
class FusionTest {
    private val fusion = RrfFusion()

    private fun dense(vararg ids: Long) = ids.mapIndexed { i, id -> Candidate(id, vector = 1.0 - i * 0.01, vectorRank = i + 1) }

    private fun lexical(vararg ids: Long) = ids.mapIndexed { i, id -> Candidate(id, bm25 = 1.0 - i * 0.01, bm25Rank = i + 1) }

    @Test
    fun `chunk at rank 2 in both legs outranks a chunk at rank 1 in one leg`() {
        // A = 10 (rank 2 in both legs); B = 20 (rank 1 dense only); C = 30 (rank 1 lexical only).
        val fused = fusion.fuse(dense(20, 10), lexical(30, 10), k = 60, returnK = 10)
        val a = fused.first { it.id == 10L }
        val b = fused.first { it.id == 20L }
        assertEquals(2.0 / 62, a.fused!!, 1e-12)
        assertEquals(1.0 / 61, b.fused!!, 1e-12)
        assertEquals(listOf(10L, 20L, 30L), fused.map { it.id })
    }

    @Test
    fun `ties are broken by lower chunk id`() {
        // 7 and 3 both appear at rank 1 of one leg only → equal scores.
        assertEquals(listOf(3L, 7L), fusion.fuse(dense(7), lexical(3), k = 60, returnK = 10).map { it.id })
    }

    @Test
    fun `k is configurable`() {
        val fused = fusion.fuse(dense(1, 2), lexical(2, 1), k = 1, returnK = 10)
        assertEquals(1.0 / 2 + 1.0 / 3, fused.first().fused!!, 1e-12)
    }

    @Test
    fun `fused list keeps both legs' scores and is truncated to return_k`() {
        val fused = fusion.fuse(dense(1, 2, 3), lexical(3, 4, 5), k = 60, returnK = 2)
        assertEquals(2, fused.size)
        val three = fusion.fuse(dense(1, 2, 3), lexical(3, 4, 5), k = 60, returnK = 10).first { it.id == 3L }
        assertEquals(3, three.vectorRank)
        assertEquals(1, three.bm25Rank)
    }

    @Test
    fun `single leg passes through in its own order with null fused`() {
        val single = fusion.single(dense(9, 4, 6), returnK = 2)
        assertEquals(listOf(9L, 4L), single.map { it.id })
        assertTrue(single.all { it.fused == null && it.bm25 == null })
    }

    @Test
    fun `rerank merge orders the head by rerank score and keeps the fused tail with null rerank`() {
        val fused = fusion.fuse(dense(*(1L..50L).toList().toLongArray()), emptyList(), k = 60, returnK = 50)
        val scores = (1L..20L).associateWith { it / 100.0 } // reversed: chunk 20 best
        val merged = RerankMerge.merge(fused, scores, candidates = 20)
        assertEquals((20L downTo 1L).toList(), merged.take(20).map { it.id })
        assertEquals(fused.drop(20).map { it.id }, merged.drop(20).map { it.id })
        merged.drop(20).forEach { assertNull(it.rerank) }
        merged.take(20).zipWithNext().forEach { (a, b) -> assertTrue(a.rerank!! >= b.rerank!!) }
    }

    @Test
    fun `rerank ties keep fused order`() {
        val fused = dense(5, 6, 7)
        assertEquals(listOf(5L, 6L, 7L), RerankMerge.merge(fused, mapOf(5L to 0.5, 6L to 0.5, 7L to 0.5), 3).map { it.id })
    }
}
