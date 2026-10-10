package dev.hybridrag.service.routing

import dev.hybridrag.service.api.SqlResult
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.jdbc.core.JdbcTemplate
import org.springframework.jdbc.core.ResultSetExtractor
import org.springframework.stereotype.Component
import org.springframework.transaction.support.TransactionTemplate
import java.sql.Array
import java.sql.ResultSet
import java.sql.SQLException
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import javax.sql.DataSource

class SqlExecutionException(
    val sqlState: String,
    cause: Throwable,
) : RuntimeException("execution_failed: $sqlState", cause)

/** Runs guarded SQL on the `rag_router` pool: read-only transaction, `limit 101` wrapper, 2 s timeout (design D7). */
@Component
class SqlExecutor(
    @Qualifier("routerDataSource") dataSource: DataSource,
    @Qualifier("routerTransactionTemplate") private val transaction: TransactionTemplate,
) {
    private val jdbc = JdbcTemplate(dataSource).apply { queryTimeout = 2 }

    fun execute(sql: String): SqlResult {
        val wrapped = "select * from ($sql) as q limit ${MAX_ROWS + 1}"
        try {
            return transaction.execute { jdbc.query(wrapped, ResultSetExtractor { result -> read(sql, result) }) }!!
        } catch (error: Exception) {
            val state = generateSequence<Throwable>(error) { it.cause }.filterIsInstance<SQLException>().firstOrNull()?.sqlState
            throw SqlExecutionException(state ?: "unknown", error)
        }
    }

    private fun read(
        sql: String,
        result: ResultSet,
    ): SqlResult {
        val meta = result.metaData
        val columns = (1..meta.columnCount).map(meta::getColumnLabel)
        val rows = mutableListOf<List<Any?>>()
        while (result.next()) rows += (1..meta.columnCount).map { jsonValue(result.getObject(it)) }
        val truncated = rows.size > MAX_ROWS
        val kept = rows.take(MAX_ROWS)
        return SqlResult(sql, columns, kept, kept.size, truncated, null)
    }

    companion object {
        const val MAX_ROWS = 100

        /** JSON-serializable primitives; dates as ISO strings. */
        fun jsonValue(value: Any?): Any? =
            when (value) {
                null, is String, is Boolean, is Int, is Long, is Short, is Double, is Float, is java.math.BigDecimal -> value
                is java.sql.Date -> value.toLocalDate().toString()
                is java.sql.Timestamp -> value.toInstant().toString()
                is LocalDate, is LocalDateTime, is OffsetDateTime -> value.toString()
                is Array -> (value.array as kotlin.Array<*>).map(::jsonValue)
                else -> value.toString()
            }
    }
}
