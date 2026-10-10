package dev.hybridrag.service.api

import com.fasterxml.jackson.annotation.JsonInclude
import java.time.LocalDate

// Request and response bodies of contract C6. Property names are camelCase here and snake_case on the wire
// (`spring.jackson.property-naming-strategy=SNAKE_CASE`); every default below is the C6 default.

data class QueryRequest(
    val question: String,
    val pipeline: Pipeline = Pipeline(),
)

data class Pipeline(
    val retrieval: RetrievalSettings = RetrievalSettings(),
    val contextual: Boolean = true,
    val filters: FilterSettings = FilterSettings(),
    val rerank: RerankSettings = RerankSettings(),
    val routing: RoutingSettings = RoutingSettings(),
    val generation: GenerationSettings = GenerationSettings(),
)

data class RetrievalSettings(
    val vector: Boolean = true,
    val bm25: Boolean = true,
    val vectorK: Int = 50,
    val bm25K: Int = 50,
    val fusion: FusionSettings = FusionSettings(),
    val returnK: Int = 50,
)

data class FusionSettings(
    val method: String = "rrf",
    val k: Int = 60,
)

data class FilterSettings(
    val enabled: Boolean = true,
    val extractFromQuestion: Boolean = true,
    val strategy: String = "auto",
    val selectivityThreshold: Double = 0.25,
    val overfetchFactor: Int = 5,
    val explicit: FilterValues? = null,
)

/** Filter values: the request's `explicit` object and the response's `applied` / `extracted` objects. */
@JsonInclude(JsonInclude.Include.NON_NULL)
data class FilterValues(
    val categories: List<String>? = null,
    val authors: List<String>? = null,
    val publishedFrom: LocalDate? = null,
    val publishedTo: LocalDate? = null,
) {
    /** Not named `isEmpty`: Jackson would serialize that as an `is_empty` property. */
    fun noneSet(): Boolean = categories.isNullOrEmpty() && authors.isNullOrEmpty() && publishedFrom == null && publishedTo == null
}

data class RerankSettings(
    val enabled: Boolean = true,
    val candidates: Int = 20,
)

data class RoutingSettings(
    val enabled: Boolean = true,
)

data class GenerationSettings(
    val enabled: Boolean = true,
    val contextK: Int = 6,
)

/** The default pipeline is the full pipeline (grid row 6); omitted request fields take these values. */
object PipelineDefaults {
    val PIPELINE = Pipeline()

    /** Jackson already fills omitted fields from the data-class defaults; this is the resolved, echoed pipeline. */
    fun resolve(request: QueryRequest): Pipeline = request.pipeline
}

data class FieldError(
    val field: String,
    val message: String,
)

class RequestValidationException(
    val errors: List<FieldError>,
) : RuntimeException(errors.joinToString("; ") { "${it.field}: ${it.message}" })

/** Validation rules of contract C6, collecting every violation as `{field, message}`. */
object QueryValidator {
    private val categoryPattern = Regex("^[a-z-]+(\\.[A-Za-z-]+)?$")

    fun validate(request: QueryRequest) {
        val errors = mutableListOf<FieldError>()
        val pipeline = request.pipeline
        val retrieval = pipeline.retrieval
        val filters = pipeline.filters
        if (request.question.trim().length !in 1..2000) {
            errors += FieldError("question", "must be 1 to 2000 characters after trimming")
        }
        if (!retrieval.vector && !retrieval.bm25) {
            errors += FieldError("pipeline.retrieval", "at least one of vector and bm25 must be true")
        }
        errors.range("pipeline.retrieval.vector_k", retrieval.vectorK, 1, 200)
        errors.range("pipeline.retrieval.bm25_k", retrieval.bm25K, 1, 200)
        errors.range("pipeline.retrieval.return_k", retrieval.returnK, 1, 200)
        if (retrieval.fusion.method != "rrf") errors += FieldError("pipeline.retrieval.fusion.method", "must be \"rrf\"")
        errors.range("pipeline.retrieval.fusion.k", retrieval.fusion.k, 1, 1000)
        if (filters.strategy !in setOf("auto", "pre", "post")) {
            errors += FieldError("pipeline.filters.strategy", "must be one of auto, pre, post")
        }
        if (filters.selectivityThreshold.isNaN() || filters.selectivityThreshold !in 0.0..1.0) {
            errors += FieldError("pipeline.filters.selectivity_threshold", "must be between 0 and 1")
        }
        errors.range("pipeline.filters.overfetch_factor", filters.overfetchFactor, 1, 20)
        if (retrieval.vectorK.toLong() * filters.overfetchFactor > 1000) {
            errors += FieldError("pipeline.filters.overfetch_factor", "vector_k × overfetch_factor must be at most 1000")
        }
        filters.explicit?.categories?.forEachIndexed { index, category ->
            if (!categoryPattern.matches(category)) {
                errors += FieldError("pipeline.filters.explicit.categories[$index]", "must match ${categoryPattern.pattern}")
            }
        }
        val from = filters.explicit?.publishedFrom
        val to = filters.explicit?.publishedTo
        if (from != null && to != null && from > to) {
            errors += FieldError("pipeline.filters.explicit.published_from", "must not be after published_to")
        }
        errors.range("pipeline.rerank.candidates", pipeline.rerank.candidates, 1, 100)
        if (pipeline.rerank.candidates > retrieval.returnK) {
            errors += FieldError("pipeline.rerank.candidates", "must not exceed pipeline.retrieval.return_k")
        }
        errors.range("pipeline.generation.context_k", pipeline.generation.contextK, 1, 20)
        if (errors.isNotEmpty()) throw RequestValidationException(errors)
    }

    private fun MutableList<FieldError>.range(
        field: String,
        value: Int,
        min: Int,
        max: Int,
    ) {
        if (value !in min..max) add(FieldError(field, "must be between $min and $max"))
    }
}

data class QueryResponse(
    val requestId: String,
    val traceId: String,
    val route: String,
    val routeSource: String,
    val pipeline: Pipeline,
    val filters: FilterResult?,
    val chunks: List<ChunkResponse>,
    val sql: SqlResult?,
    val answer: AnswerResponse,
    val degraded: List<String>,
    val timingsMs: Map<String, Double>,
    val usage: Usage,
)

data class FilterResult(
    val applied: FilterValues,
    val extracted: FilterValues,
    val strategy: String,
    val estimatedSelectivity: Double?,
    val matchingChunks: Long?,
    val underfilled: Boolean,
)

data class ChunkScores(
    val vector: Double? = null,
    val vectorRank: Int? = null,
    val bm25: Double? = null,
    val bm25Rank: Int? = null,
    val fused: Double? = null,
    val rerank: Double? = null,
)

data class ChunkResponse(
    val rank: Int,
    val chunkId: Long,
    val externalId: String,
    val ordinal: Int,
    val title: String,
    val sectionPath: String,
    val publishedOn: LocalDate,
    val primaryCategory: String,
    val content: String,
    val contextHeader: String,
    val scores: ChunkScores,
)

data class SqlResult(
    val query: String?,
    val columns: List<String>,
    val rows: List<List<Any?>>,
    val rowCount: Int,
    val truncated: Boolean,
    val error: String?,
)

data class AnswerResponse(
    val status: String,
    val httpStatus: Int?,
    val text: String?,
    val abstained: Boolean,
    val citations: List<Long>?,
    val error: String?,
) {
    companion object {
        val DISABLED = AnswerResponse("disabled", null, null, false, null, null)

        fun unavailable(error: String) = AnswerResponse("unavailable", 503, null, false, null, error)

        fun error(error: String) = AnswerResponse("error", 500, null, false, null, error)
    }
}

data class Usage(
    val embedPromptTokens: Int = 0,
    val rerankPairs: Int = 0,
    val llmPromptTokens: Int = 0,
    val llmCompletionTokens: Int = 0,
    val llmCalls: Int = 0,
)

/** No retrieval is possible: DB error in a retrieval statement, pool timeout, or embedding down without bm25 (D11). */
class RetrievalUnavailableException(
    message: String,
    cause: Throwable? = null,
) : RuntimeException(message, cause)
