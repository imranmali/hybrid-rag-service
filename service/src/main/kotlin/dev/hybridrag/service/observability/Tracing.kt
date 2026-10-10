package dev.hybridrag.service.observability

import io.opentelemetry.api.GlobalOpenTelemetry
import io.opentelemetry.api.trace.Span
import io.opentelemetry.api.trace.SpanKind
import io.opentelemetry.api.trace.StatusCode
import io.opentelemetry.api.trace.Tracer
import io.opentelemetry.context.Context
import jakarta.servlet.FilterChain
import jakarta.servlet.http.HttpServletRequest
import jakarta.servlet.http.HttpServletResponse
import org.slf4j.MDC
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.core.Ordered
import org.springframework.core.annotation.Order
import org.springframework.stereotype.Component
import org.springframework.web.filter.OncePerRequestFilter
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.Future

/** Request-scoped ids, carried in the logback MDC so every JSON log line has them (design D10). */
object TraceContext {
    const val HEADER = "X-Trace-Id"
    const val TRACE_ID = "trace_id"
    const val REQUEST_ID = "request_id"
    private val random = SecureRandom()

    fun current(): String = MDC.get(TRACE_ID) ?: randomTraceId()

    fun requestId(): String = MDC.get(REQUEST_ID) ?: UUID.randomUUID().toString()

    fun randomTraceId(): String {
        val bytes = ByteArray(16)
        random.nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }
}

@Configuration
class TracingConfiguration {
    /** The Java agent registers the global SDK; without it (tests, IDE) this is a no-op tracer. */
    @Bean
    fun ragTracer(): Tracer = GlobalOpenTelemetry.getTracer("dev.hybridrag.service")
}

/**
 * Puts `trace_id` / `request_id` into the MDC and the `X-Trace-Id` header. The agent's server span supplies the trace
 * id; without the agent a server span is started with the [Tracer] bean so stage spans still share one parent, and
 * a random 32-hex id is used when that tracer is a no-op.
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
class TraceIdFilter(
    private val tracer: Tracer,
) : OncePerRequestFilter() {
    override fun doFilterInternal(
        request: HttpServletRequest,
        response: HttpServletResponse,
        chain: FilterChain,
    ) {
        val existing = Span.current()
        val ownSpan =
            if (existing.spanContext.isValid) {
                null
            } else {
                tracer.spanBuilder("${request.method} ${request.requestURI}").setSpanKind(SpanKind.SERVER).startSpan()
            }
        val span = ownSpan ?: existing
        val traceId = span.spanContext.traceId.takeIf { span.spanContext.isValid } ?: TraceContext.randomTraceId()
        MDC.put(TraceContext.TRACE_ID, traceId)
        MDC.put(TraceContext.REQUEST_ID, UUID.randomUUID().toString())
        response.setHeader(TraceContext.HEADER, traceId)
        try {
            if (ownSpan == null) {
                chain.doFilter(request, response)
            } else {
                ownSpan.makeCurrent().use { chain.doFilter(request, response) }
            }
        } finally {
            ownSpan?.end()
            MDC.remove(TraceContext.TRACE_ID)
            MDC.remove(TraceContext.REQUEST_ID)
        }
    }
}

/**
 * Runs one pipeline stage: opens a span, records wall time into `timings_ms[name]` (also for failed attempts),
 * records failures on the span with ERROR status, and returns a [Result] so the pipeline decides the degraded outcome.
 */
class Stage(
    private val tracer: Tracer,
    val timings: MutableMap<String, Double>,
) {
    fun <T> run(
        name: String,
        action: (Span) -> T,
    ): Result<T> {
        val started = System.nanoTime()
        val span = tracer.spanBuilder(name).setSpanKind(SpanKind.INTERNAL).startSpan()
        return try {
            Result.success(span.makeCurrent().use { action(span) })
        } catch (error: Exception) {
            span.recordException(error)
            span.setStatus(StatusCode.ERROR, error.message ?: error.javaClass.simpleName)
            Result.failure(error)
        } finally {
            timings[name] = (System.nanoTime() - started) / 1_000_000.0
            span.end()
        }
    }
}

/** Virtual-thread executor whose tasks inherit the submitter's OTel context and MDC (trace id on worker log lines). */
class PropagatingExecutor : AutoCloseable {
    private val delegate: ExecutorService = Context.taskWrapping(Executors.newVirtualThreadPerTaskExecutor())

    fun <T> submit(task: () -> T): Future<T> {
        val mdc = MDC.getCopyOfContextMap()
        return delegate.submit(
            Callable {
                val previous = MDC.getCopyOfContextMap()
                if (mdc == null) MDC.clear() else MDC.setContextMap(mdc)
                try {
                    task()
                } finally {
                    if (previous == null) MDC.clear() else MDC.setContextMap(previous)
                }
            },
        )
    }

    override fun close() = delegate.close()
}
