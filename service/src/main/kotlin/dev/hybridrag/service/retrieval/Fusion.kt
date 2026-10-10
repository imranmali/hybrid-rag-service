package dev.hybridrag.service.retrieval

import org.springframework.stereotype.Component

/** One chunk moving through the pipeline with its per-stage scores (null = stage did not run / did not include it). */
data class Candidate(
    val id: Long,
    val vector: Double? = null,
    val vectorRank: Int? = null,
    val bm25: Double? = null,
    val bm25Rank: Int? = null,
    val fused: Double? = null,
    val rerank: Double? = null,
)

/** Reciprocal Rank Fusion (design D5). */
@Component
class RrfFusion {
    /** `score = Σ 1/(k + rank)` over the legs a chunk appears in; sort (score desc, id asc); truncate to [returnK]. */
    fun fuse(
        vector: List<Candidate>,
        lexical: List<Candidate>,
        k: Int,
        returnK: Int,
    ): List<Candidate> {
        val byId = LinkedHashMap<Long, Candidate>()
        vector.forEach { byId[it.id] = it }
        lexical.forEach { hit ->
            byId[hit.id] = byId[hit.id]?.copy(bm25 = hit.bm25, bm25Rank = hit.bm25Rank) ?: hit
        }
        return byId.values
            .map { it.copy(fused = listOfNotNull(it.vectorRank, it.bm25Rank).sumOf { rank -> 1.0 / (k + rank) }) }
            .sortedWith(compareByDescending<Candidate> { it.fused }.thenBy { it.id })
            .take(returnK)
    }

    /** A single leg keeps its own order, truncated to [returnK]; `fused` stays null. */
    fun single(
        leg: List<Candidate>,
        returnK: Int,
    ): List<Candidate> = leg.take(returnK)
}

object RerankMerge {
    /**
     * Final list = the first [candidates] chunks re-ordered by rerank score (ties and unscored chunks by fused rank),
     * followed by the remaining chunks in fused order with null `rerank` (design D5).
     */
    fun merge(
        fused: List<Candidate>,
        scores: Map<Long, Double>,
        candidates: Int,
    ): List<Candidate> {
        val head =
            fused
                .take(candidates)
                .mapIndexed { rank, candidate -> rank to candidate.copy(rerank = scores[candidate.id]) }
                .sortedWith(compareByDescending<Pair<Int, Candidate>> { it.second.rerank ?: Double.NEGATIVE_INFINITY }.thenBy { it.first })
                .map { it.second }
        return head + fused.drop(candidates)
    }
}
