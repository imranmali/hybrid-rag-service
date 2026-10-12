package dev.hybridrag.service.routing

import net.sf.jsqlparser.expression.Function
import net.sf.jsqlparser.parser.CCJSqlParserUtil
import net.sf.jsqlparser.schema.Table
import net.sf.jsqlparser.statement.select.ParenthesedSelect
import net.sf.jsqlparser.statement.select.PlainSelect
import net.sf.jsqlparser.statement.select.Select
import net.sf.jsqlparser.statement.select.SetOperationList
import net.sf.jsqlparser.statement.select.WithItem
import net.sf.jsqlparser.util.TablesNamesFinder
import org.springframework.stereotype.Component

class GuardRejectedException(
    reason: String,
) : RuntimeException(reason)

/** Static checks on generated SQL before it reaches the `rag_router` connection (design D7). */
@Component
class SqlGuard {
    /** Returns the SQL to execute (trimmed, one trailing `;` removed) or throws [GuardRejectedException]. */
    fun check(candidate: String): String {
        val sql = candidate.trim().removeSuffix(";").trim()
        reject(sql.isEmpty()) { "empty statement" }
        reject(sql.contains(';')) { "multiple statements" }
        reject(sql.length > MAX_LENGTH) { "longer than $MAX_LENGTH characters" }
        val statement =
            try {
                CCJSqlParserUtil.parse(sql)
            } catch (error: Exception) {
                throw GuardRejectedException("unparseable: ${error.message?.lineSequence()?.firstOrNull()}")
            }
        reject(statement !is Select) { "not a SELECT (${statement.javaClass.simpleName})" }
        reject(statement !is PlainSelect && statement !is SetOperationList && statement !is ParenthesedSelect) {
            "unsupported SELECT form (${statement.javaClass.simpleName})"
        }
        val visitor = GuardVisitor()
        try {
            visitor.getTables(statement)
        } catch (error: Exception) {
            // Fail closed: anything the visitor cannot walk is not executed.
            throw GuardRejectedException("unsupported construct: ${error.message ?: error.javaClass.simpleName}")
        }
        visitor.problems.firstOrNull()?.let { throw GuardRejectedException(it) }
        val disallowed =
            visitor.tables
                .filterNot { (schema, name) -> name in visitor.cteNames && schema == null }
                .filterNot { (schema, name) -> (schema == null || schema == "public") && name in ALLOWED_TABLES }
        reject(disallowed.isNotEmpty()) {
            "table not allowed: " + disallowed.joinToString { (schema, name) -> listOfNotNull(schema, name).joinToString(".") }
        }
        return sql
    }

    private inline fun reject(
        condition: Boolean,
        reason: () -> String,
    ) {
        if (condition) throw GuardRejectedException(reason())
    }

    /** Walks the whole AST (sub-queries, CTEs, set operations, expressions) collecting tables and violations. */
    private class GuardVisitor : TablesNamesFinder<Void>() {
        val tables = mutableListOf<Pair<String?, String>>()
        val cteNames = mutableSetOf<String>()
        val problems = mutableListOf<String>()

        init {
            init(false)
        }

        override fun <S> visit(
            withItem: WithItem<*>,
            context: S,
        ): Void? {
            withItem.aliasName?.let { cteNames += it.lowercase().trim('"') }
            if (withItem.parenthesedStatement !is ParenthesedSelect) problems += "data-modifying WITH item"
            return super.visit(withItem, context)
        }

        override fun <S> visit(
            plainSelect: PlainSelect,
            context: S,
        ): Void? {
            if (plainSelect.forMode != null ||
                plainSelect.forUpdateTable != null
            ) {
                problems += "row locking clause (FOR ${plainSelect.forMode})"
            }
            if (!plainSelect.intoTables.isNullOrEmpty()) problems += "SELECT INTO"
            return super.visit(plainSelect, context)
        }

        override fun <S> visit(
            table: Table,
            context: S,
        ): Void? {
            tables += table.schemaName?.lowercase()?.trim('"') to table.name.lowercase().trim('"')
            return super.visit(table, context)
        }

        override fun <S> visit(
            function: Function,
            context: S,
        ): Void? {
            val parts = (function.multipartName ?: listOf(function.name)).filterNotNull().map { it.lowercase().trim('"') }
            parts.firstOrNull(::forbiddenFunction)?.let { problems += "function not allowed: $it" }
            return super.visit(function, context)
        }
    }

    companion object {
        const val MAX_LENGTH = 4000
        val ALLOWED_TABLES = setOf("documents", "authors", "paper_authors", "paper_categories")

        fun forbiddenFunction(name: String): Boolean =
            name.startsWith("pg_") ||
                name.startsWith("lo_") ||
                name.startsWith("dblink") ||
                name.startsWith("query_to_xml") ||
                name == "set_config" ||
                name == "current_setting"
    }
}
