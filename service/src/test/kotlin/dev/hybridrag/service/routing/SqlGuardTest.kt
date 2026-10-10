package dev.hybridrag.service.routing

import org.junit.jupiter.api.assertDoesNotThrow
import org.junit.jupiter.api.assertThrows
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.Arguments
import org.junit.jupiter.params.provider.MethodSource
import org.junit.jupiter.params.provider.ValueSource
import kotlin.test.assertTrue

/** Task 5.2: accept / reject cases of design D13 for the JSqlParser-based guard of design D7. */
class SqlGuardTest {
    private val guard = SqlGuard()

    @ParameterizedTest
    @ValueSource(
        strings = [
            "select count(*) as n from documents",
            "select count(distinct d.id) as n from documents d join paper_categories pc on pc.document_id = d.id " +
                "where pc.category = 'cs.IR' and d.published_on between '2024-01-01' and '2024-12-31'",
            "select d.title from documents d join paper_authors pa on pa.document_id = d.id " +
                "join authors a on a.id = pa.author_id where a.name_normalized = 'jimmy lin' order by d.published_on",
            "with per_author as (select author_id, count(*) as n from paper_authors group by author_id) " +
                "select a.name, p.n from per_author p join authors a on a.id = p.author_id order by p.n desc limit 3",
            "select title from documents where primary_category = 'cs.IR' union select title from documents where primary_category = 'cs.CL'",
            "select count(*) from public.documents;",
            "select title from documents where id in (select document_id from paper_categories where category = 'cs.DB')",
            "select extract(year from published_on)::int as year, count(*) from documents group by 1 order by 1",
            "select lower(a.name), coalesce(d.abstract, '') from authors a, documents d limit 5",
            "select count(*) from generate_series(1, 10)",
            "(select title from documents order by published_on desc limit 1)",
        ],
    )
    fun `accepts safe selects`(sql: String) {
        val checked = assertDoesNotThrow { guard.check(sql) }
        assertTrue(!checked.endsWith(";"))
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("rejected")
    fun `rejects unsafe statements`(
        description: String,
        sql: String,
    ) {
        val error = assertThrows<GuardRejectedException> { guard.check(sql) }
        assertTrue(error.message!!.isNotBlank(), description)
    }

    companion object {
        @JvmStatic
        fun rejected(): List<Arguments> =
            listOf(
                Arguments.of("delete", "delete from documents"),
                Arguments.of("insert", "insert into authors(name, name_normalized) values ('a', 'a')"),
                Arguments.of("update", "update documents set title = 'x'"),
                Arguments.of("stacked statements", "select 1; drop table authors"),
                Arguments.of("comment hiding a semicolon", "select title from documents /* ; */ where true"),
                Arguments.of("line comment hiding a statement", "select 1 --;\ndrop table authors"),
                Arguments.of("data-modifying CTE", "with d as (delete from documents returning id) select id from d"),
                Arguments.of("FOR UPDATE", "select title from documents for update"),
                Arguments.of("FOR SHARE", "select title from documents for share"),
                Arguments.of("SELECT INTO", "select title into stolen from documents"),
                Arguments.of("pg_sleep", "select pg_sleep(10)"),
                Arguments.of("schema-qualified pg_sleep", "select pg_catalog.pg_sleep(10) from documents"),
                Arguments.of("set_config", "select set_config('statement_timeout', '0', false)"),
                Arguments.of("current_setting", "select current_setting('data_directory')"),
                Arguments.of("lo_import", "select lo_import('/etc/passwd')"),
                Arguments.of("dblink", "select * from dblink('host=evil', 'select 1') as t(x int)"),
                Arguments.of("query_to_xml", "select query_to_xml('select * from chunks', true, false, '')"),
                Arguments.of("chunks table", "select content from chunks"),
                Arguments.of("chunks in a sub-query", "select title from documents where id in (select document_id from chunks)"),
                Arguments.of("chunks in a CTE", "with c as (select content from chunks) select content from c"),
                Arguments.of("index_metadata", "select value from index_metadata"),
                Arguments.of("pg_catalog.pg_tables", "select tablename from pg_catalog.pg_tables"),
                Arguments.of("information_schema", "select table_name from information_schema.tables"),
                Arguments.of("other schema", "select title from private.documents"),
                Arguments.of("too long", "select title from documents where title = '" + "x".repeat(4000) + "'"),
                Arguments.of("empty", "   "),
                Arguments.of("unparseable", "selec title fro documents"),
                Arguments.of("DDL", "drop table documents"),
                Arguments.of("COPY", "copy documents to '/tmp/x'"),
            )
    }
}
