package dev.hybridrag.service.observability

import io.opentelemetry.api.GlobalOpenTelemetry
import io.opentelemetry.api.trace.SpanKind
import io.opentelemetry.api.trace.StatusCode
import io.opentelemetry.api.trace.Tracer
import jakarta.servlet.FilterChain
import jakarta.servlet.http.HttpServletRequest
import jakarta.servlet.http.HttpServletResponse
import org.slf4j.MDC
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.stereotype.Component
import org.springframework.web.filter.OncePerRequestFilter
import java.security.SecureRandom

object TraceContext {
    private val traceId = ThreadLocal<String>()

    fun get(): String = traceId.get() ?: randomTraceId().also(traceId::set)

    fun set(value: String) = traceId.set(value)

    fun clear() = traceId.remove()

    private fun randomTraceId(): String {
        val bytes = ByteArray(16)
        SecureRandom().nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }
}

@Component
class TraceIdFilter : OncePerRequestFilter() {
    override fun doFilterInternal(
        request: HttpServletRequest,
        response: HttpServletResponse,
        chain: FilterChain,
    ) {
        val otelTrace =
            io.opentelemetry.api.trace.Span
                .current()
                .spanContext.traceId
        val traceId = otelTrace.takeUnless { it == "00000000000000000000000000000000" } ?: TraceContext.get()
        TraceContext.set(traceId)
        MDC.put("trace_id", traceId)
        MDC.put("request_id", request.getHeader("X-Request-Id") ?: "")
        response.setHeader("X-Trace-Id", traceId)
        try {
            chain.doFilter(request, response)
        } finally {
            MDC.clear()
            TraceContext.clear()
        }
    }
}

@Configuration
class TracingConfiguration {
    @Bean
    fun ragTracer(): Tracer = GlobalOpenTelemetry.getTracer("dev.hybridrag.service")
}

class Stage(
    private val tracer: Tracer,
    private val timings: MutableMap<String, Double>,
) {
    fun <T> run(
        name: String,
        action: () -> T,
    ): T {
        val started = System.nanoTime()
        val span = tracer.spanBuilder(name).setSpanKind(SpanKind.INTERNAL).startSpan()
        return try {
            span.makeCurrent().use { action() }
        } catch (error: Exception) {
            span.recordException(error)
            span.setStatus(StatusCode.ERROR, error.message ?: error.javaClass.simpleName)
            throw error
        } finally {
            timings[name] = (System.nanoTime() - started) / 1_000_000.0
            span.end()
        }
    }
}
