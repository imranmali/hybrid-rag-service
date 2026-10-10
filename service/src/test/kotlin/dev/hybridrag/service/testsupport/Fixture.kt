package dev.hybridrag.service.testsupport

import com.pgvector.PGvector
import org.flywaydb.core.Flyway
import org.springframework.test.context.DynamicPropertyRegistry
import org.testcontainers.containers.PostgreSQLContainer
import java.sql.Connection
import java.sql.DriverManager
import java.time.LocalDate
import java.util.Random
import java.util.UUID
import kotlin.math.sqrt

/**
 * Deterministic test corpus (design D13): 6 documents (3 categories × 2 years) × 20 chunks, shared authors, seeded
 * L2-normalized 1024-d vectors and a header-only marker word. Chunk ids are `doc * 20 + ordinal + 1`.
 */
object Fixture {
    const val DOCUMENTS = 6
    const val CHUNKS_PER_DOCUMENT = 20
    const val CHUNKS = DOCUMENTS * CHUNKS_PER_DOCUMENT

    /** Appears only in the context headers of document 0, never in chunk content. */
    const val HEADER_MARKER = "kaleidoscope"
    const val INDEX_DIGEST = "1111111111111111111111111111111111111111111111111111111111111111"

    data class Doc(
        val index: Int,
        val category: String,
        val year: Int,
        val topic: String,
        val authors: List<String>,
    ) {
        val id: UUID = UUID.nameUUIDFromBytes("arxiv:fixture-$index".toByteArray())
        val externalId = "24%02d.%05d".format(index + 1, index + 1)
        val title = "Paper $index on $topic"
        val publishedOn: LocalDate = LocalDate.of(year, 3, 1).plusDays(index.toLong())
    }

    val namedAuthors = listOf("Jimmy Lin", "José Pérez-Gómez", "Alice Smith", "Bob Jones")
    val genericAuthors = (1..21).map { "Author %02d".format(it) }

    val docs =
        listOf(
            Doc(0, "cs.IR", 2024, "dense retrieval reranking", listOf("Jimmy Lin", "Alice Smith", "Author 01")),
            Doc(1, "cs.IR", 2025, "sparse retrieval reranking", listOf("Jimmy Lin", "Bob Jones", "Author 02")),
            Doc(2, "cs.CL", 2024, "language model translation", listOf("Jimmy Lin", "José Pérez-Gómez", "Author 03")),
            Doc(3, "cs.CL", 2025, "language model summarization", listOf("Alice Smith", "Author 04")),
            Doc(4, "cs.DB", 2024, "query optimizer indexing", listOf("Alice Smith", "Author 05")),
            Doc(5, "cs.DB", 2025, "transaction processing indexing", listOf("Bob Jones", "José Pérez-Gómez", "Author 06")),
        )

    /** Extra non-primary category of document 0. */
    const val CROSS_LISTED = "cs.LG"

    fun chunkId(
        doc: Int,
        ordinal: Int,
    ): Long = doc * CHUNKS_PER_DOCUMENT + ordinal + 1L

    fun docOf(chunkId: Long): Doc = docs[((chunkId - 1) / CHUNKS_PER_DOCUMENT).toInt()]

    fun content(
        doc: Doc,
        ordinal: Int,
    ) = "Section $ordinal of ${doc.title} studies ${doc.topic} with experiments on benchmark collection c${doc.index}x$ordinal."

    fun header(
        doc: Doc,
        ordinal: Int,
    ) = "Paper: ${doc.title} (${doc.category}, ${doc.year}). Section: ${section(ordinal)}. " +
        (if (doc.index == 0) "A $HEADER_MARKER of ideas about ${doc.topic}." else "A study of ${doc.topic}.")

    fun section(ordinal: Int) = "${ordinal / 5 + 1} Section > ${ordinal / 5 + 1}.${ordinal % 5 + 1} Part"

    /** Seeded, L2-normalized vector of a chunk; `contextual` selects `embedding_ctx`. */
    fun vector(
        chunkId: Long,
        contextual: Boolean,
    ): FloatArray {
        val random = Random(chunkId * 7919 + if (contextual) 1 else 0)
        val values = FloatArray(1024) { random.nextGaussian().toFloat() }
        val norm = sqrt(values.sumOf { (it * it).toDouble() }).toFloat()
        return FloatArray(1024) { values[it] / norm }
    }

    val allChunkIds: List<Long> = (1L..CHUNKS).toList()

    fun seed(connection: Connection) {
        connection.autoCommit = false
        val authorIds = mutableMapOf<String, Long>()
        (namedAuthors + genericAuthors).forEach { name ->
            connection.prepareStatement("insert into authors(name, name_normalized) values (?, ?) returning id").use {
                it.setString(1, name)
                it.setString(2, normalize(name))
                it.executeQuery().use { rs ->
                    rs.next()
                    authorIds[name] = rs.getLong(1)
                }
            }
        }
        docs.forEach { doc ->
            connection
                .prepareStatement(
                    """
                    insert into documents(id, external_id, arxiv_version, title, abstract, primary_category, published_on,
                      updated_on, source_url, raw_text, context_summary, content_hash, pipeline_fingerprint)
                    values (?, ?, 1, ?, ?, ?, ?, ?, ?, 'raw text', 'summary', 'hash', 'fingerprint')
                    """.trimIndent(),
                ).use {
                    it.setObject(1, doc.id)
                    it.setString(2, doc.externalId)
                    it.setString(3, doc.title)
                    it.setString(4, "Abstract about ${doc.topic}")
                    it.setString(5, doc.category)
                    it.setObject(6, doc.publishedOn)
                    it.setObject(7, doc.publishedOn)
                    it.setString(8, "https://arxiv.org/html/${doc.externalId}v1")
                    it.executeUpdate()
                }
            val categories = listOf(doc.category to true) + if (doc.index == 0) listOf(CROSS_LISTED to false) else emptyList()
            categories.forEach { (category, primary) ->
                connection.prepareStatement("insert into paper_categories(document_id, category, is_primary) values (?, ?, ?)").use {
                    it.setObject(1, doc.id)
                    it.setString(2, category)
                    it.setBoolean(3, primary)
                    it.executeUpdate()
                }
            }
            doc.authors.forEachIndexed { position, name ->
                connection.prepareStatement("insert into paper_authors(document_id, author_id, position) values (?, ?, ?)").use {
                    it.setObject(1, doc.id)
                    it.setLong(2, authorIds.getValue(name))
                    it.setInt(3, position + 1)
                    it.executeUpdate()
                }
            }
            (0 until CHUNKS_PER_DOCUMENT).forEach { ordinal ->
                val id = chunkId(doc.index, ordinal)
                connection
                    .prepareStatement(
                        """
                        insert into chunks(id, document_id, ordinal, section_path, content, context_header, token_count,
                          content_sha256, embedding, embedding_ctx)
                        values (?, ?, ?, ?, ?, ?, 20, 'sha', ?, ?)
                        """.trimIndent(),
                    ).use {
                        it.setLong(1, id)
                        it.setObject(2, doc.id)
                        it.setInt(3, ordinal)
                        it.setString(4, section(ordinal))
                        it.setString(5, content(doc, ordinal))
                        it.setString(6, header(doc, ordinal))
                        it.setObject(7, PGvector(vector(id, false)))
                        it.setObject(8, PGvector(vector(id, true)))
                        it.executeUpdate()
                    }
            }
        }
        connection.createStatement().use {
            it.execute("select setval('chunks_id_seq', $CHUNKS)")
            it.execute(
                """
                insert into index_metadata(key, value) values
                  ('embedding_model', 'bge-m3'), ('embedding_model_digest', '$INDEX_DIGEST'),
                  ('document_count', '$DOCUMENTS'), ('chunk_count', '$CHUNKS')
                """.trimIndent(),
            )
            it.execute("analyze")
        }
        connection.commit()
    }

    private fun normalize(name: String) =
        dev.hybridrag.service.retrieval.AuthorNormalizer
            .normalize(name)
}

/** A migrated, seeded `pgvector/pgvector:pg17` container (Flyway runs the real `infra/migrations`). */
class TestDatabase {
    val container: PostgreSQLContainer<*> =
        PostgreSQLContainer("pgvector/pgvector:pg17")
            .withDatabaseName("rag")
            .withUsername("rag_owner")
            .withPassword("rag_owner_dev")

    fun start(): TestDatabase {
        container.start()
        Flyway
            .configure()
            .dataSource(container.jdbcUrl, container.username, container.password)
            .locations("filesystem:../infra/migrations")
            .placeholders(
                mapOf(
                    "indexer_password" to "rag_indexer_dev",
                    "reader_password" to "rag_reader_dev",
                    "router_password" to "rag_router_dev",
                ),
            ).load()
            .migrate()
        superuser().use(Fixture::seed)
        return this
    }

    fun superuser(): Connection = DriverManager.getConnection(container.jdbcUrl, container.username, container.password)

    fun register(registry: DynamicPropertyRegistry) {
        registry.add("rag.db.url", container::getJdbcUrl)
        registry.add("rag.db.reader.user") { "rag_reader" }
        registry.add("rag.db.reader.password") { "rag_reader_dev" }
        registry.add("rag.db.router.user") { "rag_router" }
        registry.add("rag.db.router.password") { "rag_router_dev" }
    }

    companion object {
        /** Singleton shared by every integration test class (Ryuk removes it when the JVM exits). */
        val shared: TestDatabase by lazy { TestDatabase().start() }
    }
}
