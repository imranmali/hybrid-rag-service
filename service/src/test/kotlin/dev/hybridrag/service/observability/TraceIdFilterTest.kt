package dev.hybridrag.service.observability

import dev.hybridrag.service.testsupport.TestTracing
import io.opentelemetry.api.trace.SpanKind
import io.opentelemetry.api.trace.Tracer
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.slf4j.MDC
import org.springframework.mock.web.MockFilterChain
import org.springframework.mock.web.MockHttpServletRequest
import org.springframework.mock.web.MockHttpServletResponse
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * The two production paths of [TraceIdFilter]: under the OpenTelemetry Java agent the agent's server span is current
 * and its trace id must be reused (no second server span); without the agent the filter starts its own server span.
 */
class TraceIdFilterTest {
    private val tracer: Tracer = TestTracing.provider.get("dev.hybridrag.service")
    private val filter = TraceIdFilter(tracer)

    @BeforeEach
    fun reset() = TestTracing.exporter.reset()

    @AfterEach
    fun clearMdc() = MDC.clear()

    private fun run(): Pair<MockHttpServletResponse, String?> {
        var traceIdInChain: String? = null
        val chain = MockFilterChain()
        val capturing =
            jakarta.servlet.FilterChain { request, response ->
                traceIdInChain = MDC.get(TraceContext.TRACE_ID)
                chain.doFilter(request, response)
            }
        val response = MockHttpServletResponse()
        filter.doFilter(MockHttpServletRequest("POST", "/v1/query"), response, capturing)
        return response to traceIdInChain
    }

    @Test
    fun `an existing agent server span is reused - same trace id and no second server span`() {
        val agentSpan = tracer.spanBuilder("POST /v1/query").setSpanKind(SpanKind.SERVER).startSpan()
        val (response, traceIdInChain) = agentSpan.makeCurrent().use { run() }
        agentSpan.end()

        val expected = agentSpan.spanContext.traceId
        assertEquals(expected, traceIdInChain)
        assertEquals(expected, response.getHeader(TraceContext.HEADER))
        val servers = TestTracing.exporter.finishedSpanItems.filter { it.kind == SpanKind.SERVER }
        assertEquals(1, servers.size, "the filter must not start a second server span under the agent")
        assertEquals(agentSpan.spanContext.spanId, servers.single().spanId)
        assertNull(MDC.get(TraceContext.TRACE_ID), "MDC is cleared after the request")
    }

    @Test
    fun `without an agent span the filter starts its own server span and uses its trace id`() {
        val (response, traceIdInChain) = run()

        val server = TestTracing.exporter.finishedSpanItems.single { it.kind == SpanKind.SERVER }
        assertEquals("POST /v1/query", server.name)
        assertEquals(server.traceId, traceIdInChain)
        assertEquals(server.traceId, response.getHeader(TraceContext.HEADER))
        assertTrue(server.traceId.matches(Regex("[0-9a-f]{32}")))
    }
}
