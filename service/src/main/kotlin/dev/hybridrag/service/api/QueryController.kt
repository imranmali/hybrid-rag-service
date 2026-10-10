package dev.hybridrag.service.api

import com.fasterxml.jackson.annotation.JsonInclude
import com.fasterxml.jackson.databind.JsonMappingException
import com.fasterxml.jackson.databind.exc.UnrecognizedPropertyException
import dev.hybridrag.service.observability.TraceContext
import dev.hybridrag.service.pipeline.QueryPipeline
import org.slf4j.LoggerFactory
import org.springframework.http.HttpStatus
import org.springframework.http.HttpStatusCode
import org.springframework.http.MediaType
import org.springframework.http.ResponseEntity
import org.springframework.http.converter.HttpMessageNotReadableException
import org.springframework.web.ErrorResponse
import org.springframework.web.bind.annotation.ExceptionHandler
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RestController
import org.springframework.web.bind.annotation.RestControllerAdvice

@RestController
class QueryController(
    private val pipeline: QueryPipeline,
) {
    @PostMapping("/v1/query", produces = [MediaType.APPLICATION_JSON_VALUE])
    fun query(
        @RequestBody request: QueryRequest,
    ): ResponseEntity<QueryResponse> {
        QueryValidator.validate(request)
        val response = pipeline.query(request)
        // X-Trace-Id is set by TraceIdFilter for every response, including errors.
        val builder = ResponseEntity.ok()
        if (response.degraded.isNotEmpty()) builder.header("X-RAG-Degraded", response.degraded.joinToString(","))
        return builder.body(response)
    }
}

@JsonInclude(JsonInclude.Include.NON_NULL)
data class Problem(
    val type: String = "about:blank",
    val title: String,
    val status: Int,
    val detail: String,
    val errors: List<FieldError>? = null,
    val traceId: String,
)

/** Error mapping of design D11: every error is `application/problem+json` carrying `trace_id`. */
@RestControllerAdvice
class ProblemHandler {
    private val logger = LoggerFactory.getLogger(javaClass)

    @ExceptionHandler(RequestValidationException::class)
    fun validation(error: RequestValidationException) = problem(HttpStatus.BAD_REQUEST, "Request validation failed", error.errors)

    @ExceptionHandler(HttpMessageNotReadableException::class)
    fun unreadable(error: HttpMessageNotReadableException): ResponseEntity<Problem> {
        val cause = error.mostSpecificCause
        val mapping = generateSequence<Throwable>(error) { it.cause }.filterIsInstance<JsonMappingException>().firstOrNull()
        val fieldError =
            when {
                mapping is UnrecognizedPropertyException -> FieldError(path(mapping), "unknown field")
                mapping != null && mapping.path.isNotEmpty() -> FieldError(path(mapping), mapping.originalMessage.lineSequence().first())
                else -> FieldError("body", cause.message?.lineSequence()?.first() ?: "unreadable JSON body")
            }
        return problem(HttpStatus.BAD_REQUEST, "Request body could not be read: ${fieldError.field}", listOf(fieldError))
    }

    @ExceptionHandler(RetrievalUnavailableException::class)
    fun unavailable(error: RetrievalUnavailableException): ResponseEntity<Problem> {
        logger.warn("retrieval_unavailable: {}", error.message)
        return problem(HttpStatus.SERVICE_UNAVAILABLE, error.message ?: "retrieval unavailable")
    }

    @ExceptionHandler(Exception::class)
    fun unexpected(error: Exception): ResponseEntity<Problem> {
        // Spring MVC's own exceptions (404, 405, 415, …) carry their status.
        if (error is ErrorResponse) return problem(error.statusCode, error.body.detail ?: error.statusCode.toString())
        logger.error("unexpected_error", error)
        return problem(HttpStatus.INTERNAL_SERVER_ERROR, error.message ?: error.javaClass.simpleName)
    }

    private fun path(error: JsonMappingException): String =
        error.path
            .joinToString("") { ref ->
                if (ref.fieldName != null) ".${ref.fieldName}" else "[${ref.index}]"
            }.removePrefix(".")

    private fun problem(
        status: HttpStatusCode,
        detail: String,
        errors: List<FieldError>? = null,
    ): ResponseEntity<Problem> {
        val traceId = TraceContext.current()
        val title = HttpStatus.resolve(status.value())?.reasonPhrase ?: "Error"
        return ResponseEntity
            .status(status)
            .contentType(MediaType.APPLICATION_PROBLEM_JSON)
            .body(Problem(title = title, status = status.value(), detail = detail, errors = errors, traceId = traceId))
    }
}
