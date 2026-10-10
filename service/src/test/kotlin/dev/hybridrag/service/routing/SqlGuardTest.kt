package dev.hybridrag.service.routing

import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertDoesNotThrow
import org.junit.jupiter.api.assertThrows

class SqlGuardTest {
    private val guard = SqlGuard()

    @Test
    fun `accepts structured table select`() {
        assertDoesNotThrow { guard.validate("select count(*) from documents") }
    }

    @Test
    fun `rejects writes stacked queries sensitive tables and functions`() {
        listOf(
            "delete from documents",
            "select 1; drop table authors",
            "select content from chunks",
            "select pg_sleep(1) from documents",
            "select * from documents for update",
        ).forEach { sql -> assertThrows<IllegalArgumentException> { guard.validate(sql) } }
    }
}
