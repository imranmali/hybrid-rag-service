package dev.hybridrag.service.integration

import com.zaxxer.hikari.HikariDataSource
import dev.hybridrag.service.testsupport.Fixture
import dev.hybridrag.service.testsupport.IntegrationTest
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.BadSqlGrammarException
import org.springframework.jdbc.core.JdbcTemplate
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Task 1.2 smoke test + 3.1 / 3.2: the context loads against Testcontainers and the two pools are as designed. */
class DataSourceTest : IntegrationTest() {
    @Autowired
    @Qualifier("readerDataSource")
    lateinit var reader: HikariDataSource

    @Autowired
    @Qualifier("routerDataSource")
    lateinit var router: HikariDataSource

    @Test
    fun `context loads and the reader pool sees the seeded fixture`() {
        assertEquals(Fixture.CHUNKS.toLong(), JdbcTemplate(reader).queryForObject("select count(*) from chunks", Long::class.java))
    }

    @Test
    fun `reader pool cannot write`() {
        val error =
            assertThrows<Exception> {
                JdbcTemplate(reader).update("insert into authors(name, name_normalized) values ('X Y', 'x y')")
            }
        assertTrue(error.message!!.contains("read-only", ignoreCase = true) || error.message!!.contains("permission"), error.message)
    }

    @Test
    fun `router pool cannot read chunk content`() {
        val error = assertThrows<BadSqlGrammarException> { JdbcTemplate(router).queryForList("select content from chunks limit 1") }
        assertEquals("42501", error.sqlException.sqlState)
    }

    @Test
    fun `pools are sized 10 and 3, read-only, with a 1 s connection timeout`() {
        assertEquals(10, reader.maximumPoolSize)
        assertEquals(3, router.maximumPoolSize)
        assertEquals(2, reader.minimumIdle)
        assertEquals(1, router.minimumIdle)
        assertEquals(1000, reader.connectionTimeout)
        assertEquals(1000, router.connectionTimeout)
        assertTrue(reader.isReadOnly && router.isReadOnly)
        assertEquals("reader", reader.poolName)
        assertEquals("router", router.poolName)
    }
}
