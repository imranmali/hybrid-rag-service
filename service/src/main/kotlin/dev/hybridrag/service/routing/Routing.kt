package dev.hybridrag.service.routing

import com.fasterxml.jackson.databind.ObjectMapper
import dev.hybridrag.service.RagProperties
import dev.hybridrag.service.api.SqlResult
import dev.hybridrag.service.llm.OllamaClient
import net.sf.jsqlparser.parser.CCJSqlParserUtil
import net.sf.jsqlparser.statement.select.Select
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.core.ResultSetExtractor
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.stereotype.Component
import org.springframework.transaction.support.TransactionTemplate
import java.sql.Date
import java.sql.ResultSet
import java.time.LocalDate

data class RouteDecision(
    val route: String,
    val source: String,
)

@Component
class RouteClassifier(
    private val ollama: OllamaClient,
    private val mapper: ObjectMapper,
    private val properties: RagProperties,
) {
    fun classify(question: String): RouteDecision {
        val system =
            """
            route-v1: Classify a question as JSON {"route":"sql"} only for count, list, rank, or aggregate
            questions over paper titles, authors, categories, and publication dates. Use {"route":"vector"} for
            questions about what papers say, propose, find, or evaluate.
            """.trimIndent()
        val reply = ollama.chat(system, question, true, 32, properties.timeouts.routeMs).content
        val route = mapper.readTree(reply).path("route").asText()
        require(route == "sql" || route == "vector") { "unrecognised route" }
        return RouteDecision(route, "llm")
    }
}

@Component
class SqlGenerator(
    private val ollama: OllamaClient,
    private val mapper: ObjectMapper,
    private val properties: RagProperties,
) {
    fun generate(question: String): String {
        val system =
            """
            sqlgen-v1: Return JSON {"sql":"..."} with one PostgreSQL SELECT only; never use SELECT *.
            Allowed tables: documents(id, external_id, arxiv_version, title, abstract, primary_category,
            published_on, updated_on, source_url), authors, paper_authors, paper_categories. Match normalized
            author names through authors.name_normalized; use paper_categories for categories.
            """.trimIndent()
        return mapper
            .readTree(ollama.chat(system, question, true, 256, properties.timeouts.sqlGenerateMs).content)
            .path("sql")
            .asText()
            .also { require(it.isNotBlank()) { "missing generated sql" } }
    }
}

@Component
class SqlGuard {
    private val allowedTables = setOf("documents", "authors", "paper_authors", "paper_categories")
    private val forbiddenFunctions =
        Regex("(?i)\\b(pg_[a-z0-9_]*|lo_[a-z0-9_]*|dblink[a-z0-9_]*|set_config|current_setting|query_to_xml[a-z0-9_]*)\\s*\\(")

    fun validate(candidate: String): String {
        val sql = candidate.trim().removeSuffix(";").trim()
        require(sql.isNotBlank()) { "empty SQL" }
        require(sql.length <= 4000) { "SQL exceeds 4000 characters" }
        require(!sql.contains(';')) { "multiple statements are not allowed" }
        require(!Regex("(?i)\\b(for\\s+update|select\\s+.*\\s+into\\s+)").containsMatchIn(sql)) { "locking or SELECT INTO is not allowed" }
        require(!forbiddenFunctions.containsMatchIn(sql)) { "disallowed function" }
        val statement = CCJSqlParserUtil.parse(sql)
        require(statement is Select) { "only SELECT statements are allowed" }
        val tables =
            Regex("(?i)\\b(?:from|join)\\s+(?:public\\.)?([a-z_]+)")
                .findAll(sql)
                .map { it.groupValues[1].lowercase() }
                .toSet()
        require(tables.all { it in allowedTables }) { "disallowed table: ${(tables - allowedTables).joinToString()}" }
        return sql
    }
}

@Component
class SqlExecutor(
    @Qualifier("routerJdbcTemplate") private val jdbc: NamedParameterJdbcTemplate,
    @Qualifier("readerTransactionTemplate") private val transaction: TransactionTemplate,
) {
    fun execute(query: String): SqlResult {
        val wrapped = "select * from ($query) as q limit 101"
        return try {
            transaction.execute<SqlResult> {
                jdbc.jdbcTemplate.query(
                    wrapped,
                    ResultSetExtractor { result ->
                        val metadata = result.metaData
                        val columns = (1..metadata.columnCount).map(metadata::getColumnLabel)
                        val rows = mutableListOf<List<Any?>>()
                        while (result.next()) rows += (1..metadata.columnCount).map { jsonValue(result, it) }
                        SqlResult(query, columns, rows.take(100), rows.size, rows.size > 100, null)
                    },
                )
            } ?: throw IllegalStateException("SQL transaction returned null")
        } catch (error: Exception) {
            SqlResult(query, emptyList(), emptyList(), 0, false, "execution_failed: ${error.sqlState()}")
        }
    }

    private fun jsonValue(
        result: ResultSet,
        column: Int,
    ): Any? =
        when (val value = result.getObject(column)) {
            is Date -> value.toLocalDate().toString()
            is LocalDate -> value.toString()
            is Number, is Boolean, is String -> value
            null -> null
            else -> value.toString()
        }

    private fun Throwable.sqlState(): String {
        var current: Throwable? = this
        while (current != null) {
            if (current is java.sql.SQLException) return current.sqlState ?: "unknown"
            current = current.cause
        }
        return "unknown"
    }
}
