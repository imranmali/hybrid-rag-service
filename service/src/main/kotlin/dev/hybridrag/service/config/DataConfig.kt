package dev.hybridrag.service.config

import com.zaxxer.hikari.HikariConfig
import com.zaxxer.hikari.HikariDataSource
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Primary
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.jdbc.datasource.DataSourceTransactionManager
import org.springframework.transaction.support.TransactionTemplate

@Configuration
class DataConfig(
    private val properties: RagProperties,
) {
    // HikariCP sizing (reader pool = 10):
    // - Postgres here is a 4-vCPU container; its throughput peaks around (2 x cores) + 1 ≈ 9 active
    //   connections (PostgreSQL wiki "number of connections"); more connections only add contention.
    // - A query holds a connection only for short statements (estimate ≤ 500 ms budget, each leg
    //   ≤ 1.5 s budget, typically < 20 ms) and never across model calls, so concurrency at the DB is
    //   ~2 connections per in-flight retrieval, not per in-flight request.
    // - The k6 retrieval-only scenario (~20 rps x ~3 statements x ~20 ms ≈ 1.2 busy connections by
    //   Little's law) leaves 8x burst headroom; LLM-bound traffic is throttled upstream by Ollama.
    // - connectionTimeout = 1 s: if the pool is exhausted we fail fast into a 503 rather than queue
    //   behind a slow model-bound request.
    // Router pool = 3: generated SQL is rare (aggregate questions) and capped at 2 s by the role.
    @Bean(destroyMethod = "close")
    @Primary
    fun readerDataSource(): HikariDataSource = pool("reader", properties.db.reader, minimumIdle = 2)

    @Bean(destroyMethod = "close")
    fun routerDataSource(): HikariDataSource = pool("router", properties.db.router, minimumIdle = 1)

    @Bean
    @Primary
    fun readerJdbcTemplate(
        @Qualifier("readerDataSource") source: HikariDataSource,
    ) = NamedParameterJdbcTemplate(source)

    @Bean
    fun routerJdbcTemplate(
        @Qualifier("routerDataSource") source: HikariDataSource,
    ) = NamedParameterJdbcTemplate(source)

    @Bean
    @Primary
    fun readerTransactionManager(
        @Qualifier("readerDataSource") source: HikariDataSource,
    ) = DataSourceTransactionManager(source)

    @Bean
    fun routerTransactionManager(
        @Qualifier("routerDataSource") source: HikariDataSource,
    ) = DataSourceTransactionManager(source)

    /** Short read-only transaction on the reader pool; always ends before any HTTP call. */
    @Bean
    @Primary
    fun readerTransactionTemplate(
        @Qualifier("readerTransactionManager") manager: DataSourceTransactionManager,
    ) = TransactionTemplate(manager).apply { isReadOnly = true }

    /** Read-only transaction on the router pool for guarded generated SQL. */
    @Bean
    fun routerTransactionTemplate(
        @Qualifier("routerTransactionManager") manager: DataSourceTransactionManager,
    ) = TransactionTemplate(manager).apply { isReadOnly = true }

    private fun pool(
        name: String,
        credentials: RagProperties.PoolProperties,
        minimumIdle: Int,
    ): HikariDataSource =
        HikariDataSource(
            HikariConfig().apply {
                jdbcUrl = properties.db.url
                username = credentials.user
                password = credentials.password
                maximumPoolSize = credentials.poolSize
                this.minimumIdle = minimumIdle
                connectionTimeout = 1000
                maxLifetime = 30 * 60 * 1000L
                isReadOnly = true
                poolName = name
                // Start even when Postgres is down; requests then fail fast into a 503.
                initializationFailTimeout = -1
            },
        )
}
