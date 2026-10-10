package dev.hybridrag.service.resilience

import dev.hybridrag.service.config.RagProperties
import io.github.resilience4j.circuitbreaker.CallNotPermittedException
import io.github.resilience4j.circuitbreaker.CircuitBreaker
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig
import io.github.resilience4j.retry.Retry
import io.github.resilience4j.retry.RetryConfig
import net.logstash.logback.argument.StructuredArguments.kv
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Component
import java.net.ConnectException
import java.net.http.HttpConnectTimeoutException
import java.net.http.HttpTimeoutException
import java.time.Duration
import java.util.function.Supplier

/** The three protected dependencies; `llm` is shared by route, SQL generation and answer generation. */
enum class Dependency(
    val id: String,
) {
    EMBEDDING("embedding"),
    RERANKER("reranker"),
    LLM("llm"),
}

/** Non-2xx answer from a dependency (5xx is retried, every status is a breaker failure). */
class DependencyHttpException(
    val dependency: Dependency,
    val status: Int,
) : RuntimeException("${dependency.id} returned HTTP $status")

/** 2xx answer whose body does not have the contract shape. */
class MalformedResponseException(
    dependency: Dependency,
    detail: String,
) : RuntimeException("${dependency.id} returned a malformed body: $detail")

object RetryPolicy {
    /** Retry only connect failures and HTTP 5xx; never read timeouts, 4xx or an open breaker (contract C11). */
    fun isRetryable(error: Throwable): Boolean {
        if (error is CallNotPermittedException) return false
        if (error is DependencyHttpException) return error.status >= 500
        return generateSequence(error) { it.cause }.any { it is ConnectException || it is HttpConnectTimeoutException }
    }

    /** True when the failure was a read timeout (the hard stage timeout). */
    fun isTimeout(error: Throwable): Boolean =
        generateSequence(error) { it.cause }.any { it is HttpTimeoutException && it !is HttpConnectTimeoutException }
}

/** `Retry(CircuitBreaker(call))` per dependency (design D9). */
@Component
class DependencyGuards(
    properties: RagProperties,
) {
    private val logger = LoggerFactory.getLogger(javaClass)
    private val settings = properties.resilience
    private val breakers = Dependency.entries.associateWith(::newBreaker)
    private val retries = Dependency.entries.associateWith(::newRetry)

    fun <T> call(
        dependency: Dependency,
        action: () -> T,
    ): T {
        val guarded = CircuitBreaker.decorateSupplier(breakers.getValue(dependency), Supplier(action))
        return Retry.decorateSupplier(retries.getValue(dependency), guarded).get()
    }

    fun breaker(dependency: Dependency): CircuitBreaker = breakers.getValue(dependency)

    /** Test hook: close every breaker so test cases do not leak breaker state into each other. */
    fun resetAll() = breakers.values.forEach(CircuitBreaker::reset)

    private fun newBreaker(dependency: Dependency): CircuitBreaker {
        val config =
            CircuitBreakerConfig
                .custom()
                .slidingWindowType(CircuitBreakerConfig.SlidingWindowType.COUNT_BASED)
                .slidingWindowSize(settings.slidingWindowSize)
                .minimumNumberOfCalls(settings.minimumNumberOfCalls)
                .failureRateThreshold(settings.failureRateThreshold)
                .waitDurationInOpenState(Duration.ofMillis(settings.waitDurationInOpenStateMs))
                .permittedNumberOfCallsInHalfOpenState(settings.permittedNumberOfCallsInHalfOpenState)
                .automaticTransitionFromOpenToHalfOpenEnabled(true)
                .slowCallRateThreshold(100f)
                .build()
        return CircuitBreaker.of(dependency.id, config).also { breaker ->
            breaker.eventPublisher.onStateTransition { event ->
                logger.warn(
                    "breaker_transition",
                    kv("event", "breaker_transition"),
                    kv("dependency", dependency.id),
                    kv("from", event.stateTransition.fromState.name),
                    kv("to", event.stateTransition.toState.name),
                )
            }
        }
    }

    private fun newRetry(dependency: Dependency): Retry {
        val wait =
            when (dependency) {
                Dependency.EMBEDDING -> settings.embeddingRetryWaitMs
                Dependency.RERANKER -> settings.rerankerRetryWaitMs
                Dependency.LLM -> settings.llmRetryWaitMs
            }
        val config =
            RetryConfig
                .custom<Any>()
                .maxAttempts(settings.maxAttempts)
                .waitDuration(Duration.ofMillis(wait))
                .retryOnException(RetryPolicy::isRetryable)
                .ignoreExceptions(CallNotPermittedException::class.java)
                .build()
        return Retry.of(dependency.id, config)
    }
}
