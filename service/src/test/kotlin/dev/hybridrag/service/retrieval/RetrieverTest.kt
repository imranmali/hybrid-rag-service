package dev.hybridrag.service.retrieval

import dev.hybridrag.service.config.RagProperties
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import org.junit.jupiter.api.Test
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.transaction.support.TransactionTemplate
import java.util.Random
import kotlin.math.sqrt
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 3.3: dense and lexical legs against the seeded pgvector database. */
class RetrieverTest : IntegrationTest() {
    @Autowired
    lateinit var vectorRetriever: VectorRetriever

    @Autowired
    lateinit var lexicalRetriever: LexicalRetriever

    @Autowired
    @Qualifier("readerJdbcTemplate")
    lateinit var jdbc: NamedParameterJdbcTemplate

    @Autowired
    @Qualifier("readerTransactionTemplate")
    lateinit var transaction: TransactionTemplate

    @Test
    fun `identical vector is rank 1 with similarity 1`() {
        val target = Fixture.chunkId(2, 7)
        val result =
            vectorRetriever.retrieve(
                Fixture.vector(target, true),
                contextual = true,
                vectorK = 10,
                overfetchFactor = 5,
                plan = null,
            )
        val first = result.candidates.first()
        assertEquals(target, first.id)
        assertEquals(1, first.vectorRank)
        assertEquals(1.0, first.vector!!, 1e-4)
    }

    @Test
    fun `plain column variant is used when contextual is false`() {
        val target = Fixture.chunkId(4, 3)
        val result =
            vectorRetriever.retrieve(
                Fixture.vector(target, false),
                contextual = false,
                vectorK = 5,
                overfetchFactor = 5,
                plan = null,
            )
        assertEquals(target, result.candidates.first().id)
        assertEquals(1.0, result.candidates.first().vector!!, 1e-4)
    }

    @Test
    fun `vector_k 50 returns 50 candidates (more than the pgvector default ef_search of 40)`() {
        val result = vectorRetriever.retrieve(queryVector(1), contextual = true, vectorK = 50, overfetchFactor = 5, plan = null)
        assertEquals(50, result.candidates.size)
        assertEquals((1..50).toList(), result.candidates.map { it.vectorRank })
        assertEquals(false, result.underfilled)
    }

    @Test
    fun `exact mode returns the same ids as a brute-force cosine sort`() {
        val exact =
            VectorRetriever(jdbc, transaction, RagProperties(retrieval = RagProperties.RetrievalProperties(exactVectorSearch = true)))
        val query = queryVector(2)
        val result = exact.retrieve(query, contextual = false, vectorK = 50, overfetchFactor = 5, plan = null)
        assertEquals(bruteForce(query, Fixture.allChunkIds, contextual = false).take(50), result.candidates.map { it.id })
    }

    @Test
    fun `EXPLAIN of the plain query uses the HNSW index`() {
        // On a 120-row table a seq scan is cheaper, so it is disabled here: the assertion is that the query shape
        // (ORDER BY distance only + LIMIT) lets the planner use chunks_embedding_hnsw, as on the real corpus.
        val plan =
            transaction.execute {
                setLocal(jdbc, "enable_seqscan", "off")
                setLocal(jdbc, "hnsw.ef_search", "100")
                val vector = com.pgvector.PGvector(queryVector(3))
                jdbc.jdbcTemplate
                    .queryForList(
                        "explain select c.id, 1 - (c.embedding <=> ?) as sim from chunks c order by c.embedding <=> ? limit 50",
                        String::class.java,
                        vector,
                        vector,
                    ).joinToString("\n")
            }!!
        assertTrue(plan.contains("chunks_embedding_hnsw"), plan)
    }

    @Test
    fun `header-only marker word is found with tsv_ctx and not with tsv`() {
        val contextual = lexicalRetriever.retrieve(Fixture.HEADER_MARKER, contextual = true, bm25K = 50, plan = null)
        val plain = lexicalRetriever.retrieve(Fixture.HEADER_MARKER, contextual = false, bm25K = 50, plan = null)
        assertEquals(Fixture.CHUNKS_PER_DOCUMENT, contextual.size)
        assertTrue(contextual.all { Fixture.docOf(it.id).index == 0 })
        assertTrue(plain.isEmpty())
    }

    @Test
    fun `stop-word-only question returns no lexical candidates`() {
        assertTrue(lexicalRetriever.retrieve("the and of to", contextual = true, bm25K = 50, plan = null).isEmpty())
    }

    @Test
    fun `OR semantics - a chunk containing one of three terms matches, ranked by ts_rank_cd then id`() {
        val hits = lexicalRetriever.retrieve("reranking zebra quokka", contextual = false, bm25K = 50, plan = null)
        assertEquals(2 * Fixture.CHUNKS_PER_DOCUMENT, hits.size)
        assertTrue(hits.all { Fixture.docOf(it.id).index in 0..1 })
        assertEquals((1..hits.size).toList(), hits.map { it.bm25Rank })
        hits.zipWithNext().forEach { (a, b) -> assertTrue(a.bm25!! > b.bm25!! || (a.bm25 == b.bm25 && a.id < b.id)) }
    }

    @Test
    fun `bm25_k bounds the lexical leg`() {
        assertEquals(7, lexicalRetriever.retrieve("benchmark collection", contextual = false, bm25K = 7, plan = null).size)
    }

    companion object {
        fun queryVector(seed: Long): FloatArray {
            val random = Random(seed)
            val values = FloatArray(1024) { random.nextGaussian().toFloat() }
            val norm = sqrt(values.sumOf { (it * it).toDouble() }).toFloat()
            return FloatArray(1024) { values[it] / norm }
        }

        /** Chunk ids sorted by cosine similarity desc, ties by id. */
        fun bruteForce(
            query: FloatArray,
            ids: List<Long>,
            contextual: Boolean,
        ): List<Long> =
            ids
                .map { id ->
                    val v = Fixture.vector(id, contextual)
                    id to (0 until 1024).sumOf { (query[it] * v[it]).toDouble() }
                }.sortedWith(compareByDescending<Pair<Long, Double>> { it.second }.thenBy { it.first })
                .map { it.first }
    }
}
