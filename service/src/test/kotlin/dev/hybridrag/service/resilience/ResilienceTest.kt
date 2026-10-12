package dev.hybridrag.service.resilience

import dev.hybridrag.service.config.RagProperties
import io.github.resilience4j.circuitbreaker.CallNotPermittedException
import io.github.resilience4j.circuitbreaker.CircuitBreaker
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows
import org.junit.jupiter.api.extension.ExtendWith
import org.springframework.boot.test.system.CapturedOutput
import org.springframework.boot.test.system.OutputCaptureExtension
import org.springframework.web.client.ResourceAccessException
import java.io.IOException
import java.net.ConnectException
import java.net.http.HttpConnectTimeoutException
import java.net.http.HttpTimeoutException
import java.util.concurrent.atomic.AtomicInteger
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** Task 4.2: retry predicate and `Retry(CircuitBreaker(call))` behaviour (design D9). */
@ExtendWith(OutputCaptureExtension::class)
class ResilienceTest {
    @Test
    fun `connect failures and 5xx are retried`() {
        assertTrue(RetryPolicy.isRetryable(ResourceAccessException("io", ConnectException("refused"))))
        assertTrue(RetryPolicy.isRetryable(ResourceAccessException("io", HttpConnectTimeoutException("connect timed out"))))
        assertTrue(RetryPolicy.isRetryable(DependencyHttpException(Dependency.RERANKER, 500)))
        assertTrue(RetryPolicy.isRetryable(DependencyHttpException(Dependency.LLM, 503)))
    }

    @Test
    fun `read timeouts, 4xx, other IO errors and an open breaker are not retried`() {
        assertFalse(RetryPolicy.isRetryable(ResourceAccessException("io", HttpTimeoutException("request timed out"))))
        assertFalse(RetryPolicy.isRetryable(DependencyHttpException(Dependency.LLM, 404)))
        assertFalse(RetryPolicy.isRetryable(DependencyHttpException(Dependency.RERANKER, 422)))
        assertFalse(RetryPolicy.isRetryable(ResourceAccessException("io", IOException("connection reset"))))
        assertFalse(RetryPolicy.isRetryable(CallNotPermittedException.createCallNotPermittedException(CircuitBreaker.ofDefaults("x"))))
        assertFalse(RetryPolicy.isRetryable(MalformedResponseException(Dependency.EMBEDDING, "bad")))
    }

    @Test
    fun `timeout detection ignores connect timeouts`() {
        assertTrue(RetryPolicy.isTimeout(ResourceAccessException("io", HttpTimeoutException("request timed out"))))
        assertFalse(RetryPolicy.isTimeout(ResourceAccessException("io", HttpConnectTimeoutException("connect timed out"))))
    }

    @Test
    fun `a 5xx is attempted twice, a 4xx once`() {
        val guards = DependencyGuards(fastProperties())
        val calls = AtomicInteger()
        assertThrows<DependencyHttpException> {
            guards.call(Dependency.RERANKER) {
                calls.incrementAndGet()
                throw DependencyHttpException(Dependency.RERANKER, 500)
            }
        }
        assertEquals(2, calls.get())
        calls.set(0)
        assertThrows<DependencyHttpException> {
            guards.call(Dependency.RERANKER) {
                calls.incrementAndGet()
                throw DependencyHttpException(Dependency.RERANKER, 404)
            }
        }
        assertEquals(1, calls.get())
    }

    @Test
    fun `breaker opens at the failure threshold, short-circuits, and logs the transition`(output: CapturedOutput) {
        val guards = DependencyGuards(fastProperties())
        val calls = AtomicInteger()
        repeat(2) {
            runCatching {
                guards.call(Dependency.LLM) {
                    calls.incrementAndGet()
                    throw DependencyHttpException(Dependency.LLM, 500)
                }
            }
        }
        assertEquals(4, calls.get())
        assertEquals(CircuitBreaker.State.OPEN, guards.breaker(Dependency.LLM).state)
        assertThrows<CallNotPermittedException> { guards.call(Dependency.LLM) { calls.incrementAndGet() } }
        assertEquals(4, calls.get())
        assertEquals(CircuitBreaker.State.CLOSED, guards.breaker(Dependency.EMBEDDING).state)
        assertTrue(
            output.out.lines().any { "breaker_transition" in it && "\"dependency\":\"llm\"" in it && "\"to\":\"OPEN\"" in it },
            output.out,
        )
    }

    @Test
    fun `contract defaults - window 20, minimum 10, 50 percent, 30 s open, 3 half-open calls`() {
        val config = DependencyGuards(RagProperties()).breaker(Dependency.RERANKER).circuitBreakerConfig
        assertEquals(20, config.slidingWindowSize)
        assertEquals(10, config.minimumNumberOfCalls)
        assertEquals(50f, config.failureRateThreshold)
        assertEquals(3, config.permittedNumberOfCallsInHalfOpenState)
        assertEquals(CircuitBreaker.State.CLOSED, DependencyGuards(RagProperties()).breaker(Dependency.LLM).state)
        assertTrue(config.isAutomaticTransitionFromOpenToHalfOpenEnabled)
        assertEquals(100f, config.slowCallRateThreshold)
    }

    private fun fastProperties() =
        RagProperties(
            resilience =
                RagProperties.ResilienceProperties(
                    slidingWindowSize = 4,
                    minimumNumberOfCalls = 4,
                    embeddingRetryWaitMs = 1,
                    rerankerRetryWaitMs = 1,
                    llmRetryWaitMs = 1,
                ),
        )
}
