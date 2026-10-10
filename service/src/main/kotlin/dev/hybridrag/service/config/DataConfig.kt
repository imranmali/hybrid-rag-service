package dev.hybridrag.service.config

import com.zaxxer.hikari.HikariConfig
import com.zaxxer.hikari.HikariDataSource
import dev.hybridrag.service.RagProperties
import org.springframework.beans.factory.annotation.Qualifier
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Primary
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate
import org.springframework.transaction.PlatformTransactionManager
import org.springframework.transaction.support.TransactionTemplate
import javax.sql.DataSource

@Configuration
class DataConfig(
    private val properties: RagProperties,
) {
    // HikariCP sizing (reader pool = 10): Postgres is a 4-vCPU container, whose useful
    // concurrency is about (2 × cores) + 1. Retrieval statements are short and connections
    // are returned before model calls, leaving burst headroom without adding contention.
    @Bean(destroyMethod = "close")
    @Primary
    fun readerDataSource(): HikariDataSource =
        dataSource(
            "reader",
            properties.db.reader.user,
            properties.db.reader.password,
            properties.db.reader.poolSize,
            2,
        )

    // Router pool = 3: generated aggregate SQL is rare and independently capped at 2 seconds.
    @Bean(destroyMethod = "close")
    fun routerDataSource(): HikariDataSource =
        dataSource(
            "router",
            properties.db.router.user,
            properties.db.router.password,
            properties.db.router.poolSize,
            1,
        )

    @Bean
    fun readerJdbcTemplate(
        @Qualifier("readerDataSource") source: DataSource,
    ) = NamedParameterJdbcTemplate(source)

    @Bean
    fun routerJdbcTemplate(
        @Qualifier("routerDataSource") source: DataSource,
    ) = NamedParameterJdbcTemplate(source)

    @Bean
    fun readerTransactionTemplate(
        @Qualifier("transactionManager") manager: PlatformTransactionManager,
    ): TransactionTemplate = TransactionTemplate(manager).also { it.isReadOnly = true }

    private fun dataSource(
        name: String,
        user: String,
        password: String,
        maximum: Int,
        minimum: Int,
    ): HikariDataSource {
        val config =
            HikariConfig().apply {
                jdbcUrl = properties.db.url
                username = user
                this.password = password
                maximumPoolSize = maximum
                minimumIdle = minimum
                connectionTimeout = 1000
                maxLifetime = 30 * 60 * 1000
                isReadOnly = true
                poolName = name
            }
        return HikariDataSource(config)
    }
}
