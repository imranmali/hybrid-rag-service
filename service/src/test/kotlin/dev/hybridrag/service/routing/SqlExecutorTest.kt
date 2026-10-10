package dev.hybridrag.service.routing

import com.zaxxer.hikari.HikariDataSource
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import dev.hybridrag.service.testsupport.WireMockFixture
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.BadSqlGrammarException
import org.springframework.jdbc.core.JdbcTemplate
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 5.3: guarded SQL on the `rag_router` pool. */
class SqlExecutorTest : IntegrationTest() {
    @Autowired
    lateinit var executor: SqlExecutor

    @Autowired
    @Qualifier("routerDataSource")
    lateinit var router: HikariDataSource

    @Test
    fun `count query returns the fixture's number`() {
        val result =
            executor.execute(
                "select count(distinct pa.document_id) as n from paper_authors pa join authors a on a.id = pa.author_id " +
                    "where a.name_normalized = 'jimmy lin'",
            )
        assertEquals(listOf("n"), result.columns)
        assertEquals(listOf(listOf<Any?>(3L)), result.rows)
        assertEquals(false, result.truncated)
    }

    @Test
    fun `a 1050-row query returns 100 rows and truncated`() {
        val result =
            executor.execute(
                "select a.name, pc.category, d.title from authors a cross join paper_categories pc cross join documents d",
            )
        assertEquals(100, result.rows.size)
        assertEquals(100, result.rowCount)
        assertEquals(true, result.truncated)
    }

    @Test
    fun `values are JSON-safe with ISO dates`() {
        val result = executor.execute("select published_on, external_id, arxiv_version from documents order by published_on limit 1")
        assertEquals(listOf(Fixture.docs[0].publishedOn.toString(), Fixture.docs[0].externalId, 1), result.rows.single())
    }

    @Test
    fun `slow query is cancelled after 2 s and the request falls back to vector with sql_fallback`() {
        WireMockFixture.stubRoute("sql")
        WireMockFixture.stubSqlGen("select count(*) from generate_series(1, 1e10)")
        WireMockFixture.stubEmbed(Fixture.vector(1, true))
        WireMockFixture.stubRerankReversing()
        val started = System.nanoTime()
        val result = query("How many numbers are there?", """{"generation": {"enabled": false}}""")
        val elapsedMs = (System.nanoTime() - started) / 1_000_000
        assertEquals(200, result.status)
        assertTrue(result.json["sql"]["error"].asText().startsWith("execution_failed"), result.json["sql"].toString())
        assertEquals("execution_failed: 57014", result.json["sql"]["error"].asText())
        assertEquals(listOf("sql_fallback"), result.json["degraded"].map { it.asText() })
        assertEquals("vector", result.json["route"].asText())
        assertTrue(result.json["timings_ms"]["sql.execute"].asDouble() in 1500.0..4000.0, result.json["timings_ms"].toString())
        assertTrue(elapsedMs < 8000, "took $elapsedMs ms")
    }

    @Test
    fun `executing an error raises SqlExecutionException with the SQLState`() {
        val error = assertThrows<SqlExecutionException> { executor.execute("select raw_text from documents") }
        assertEquals("42501", error.sqlState)
    }

    @Test
    fun `privilege boundary holds without the guard - router cannot read raw_text`() {
        val error = assertThrows<BadSqlGrammarException> { JdbcTemplate(router).queryForList("select raw_text from documents") }
        assertEquals("42501", error.sqlException.sqlState)
    }
}
