package dev.hybridrag.service.api

import com.fasterxml.jackson.annotation.JsonInclude
import java.time.LocalDate

data class QueryRequest(
    val question: String = "",
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
    val explicit: ExplicitFilters? = null,
)

data class ExplicitFilters(
    val categories: List<String>? = null,
    val authors: List<String>? = null,
    val publishedFrom: LocalDate? = null,
    val publishedTo: LocalDate? = null,
)

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

object PipelineDefaults {
    fun resolve(request: QueryRequest): Pipeline = request.pipeline
}

data class FieldError(
    val field: String,
    val message: String,
)

class RequestValidationException(
    val errors: List<FieldError>,
) : RuntimeException(errors.joinToString("; ") { "${it.field}: ${it.message}" })

object QueryValidator {
    private val categoryPattern = Regex("^[a-z-]+(\\.[A-Za-z-]+)?$")

    fun validate(request: QueryRequest) {
        val pipeline = request.pipeline
        val retrieval = pipeline.retrieval
        val errors =
            buildList {
                if (request.question.trim().length !in 1..2000) {
                    add(FieldError("question", "must contain 1 to 2000 non-whitespace characters"))
                }
                if (!retrieval.vector && !retrieval.bm25) {
                    add(FieldError("pipeline.retrieval", "vector or bm25 must be enabled"))
                }
                range("pipeline.retrieval.vector_k", retrieval.vectorK, 1, 200, this)
                range("pipeline.retrieval.bm25_k", retrieval.bm25K, 1, 200, this)
                range("pipeline.retrieval.return_k", retrieval.returnK, 1, 200, this)
                if (retrieval.fusion.method != "rrf") {
                    add(FieldError("pipeline.retrieval.fusion.method", "must be rrf"))
                }
                range("pipeline.retrieval.fusion.k", retrieval.fusion.k, 1, 1000, this)
                if (pipeline.filters.strategy !in setOf("auto", "pre", "post")) {
                    add(FieldError("pipeline.filters.strategy", "must be auto, pre, or post"))
                }
                if (pipeline.filters.selectivityThreshold !in 0.0..1.0) {
                    add(FieldError("pipeline.filters.selectivity_threshold", "must be between 0 and 1"))
                }
                range("pipeline.filters.overfetch_factor", pipeline.filters.overfetchFactor, 1, 20, this)
                if (retrieval.vectorK * pipeline.filters.overfetchFactor > 1000) {
                    add(FieldError("pipeline.filters.overfetch_factor", "vector_k × overfetch_factor must not exceed 1000"))
                }
                pipeline.filters.explicit?.categories?.forEachIndexed { index, category ->
                    if (!categoryPattern.matches(category)) {
                        add(FieldError("pipeline.filters.explicit.categories[$index]", "must be a category code"))
                    }
                }
                val explicit = pipeline.filters.explicit
                if (explicit?.publishedFrom != null && explicit.publishedTo != null && explicit.publishedFrom > explicit.publishedTo) {
                    add(FieldError("pipeline.filters.explicit", "published_from must not exceed published_to"))
                }
                range("pipeline.rerank.candidates", pipeline.rerank.candidates, 1, 100, this)
                if (pipeline.rerank.candidates > retrieval.returnK) {
                    add(FieldError("pipeline.rerank.candidates", "must not exceed return_k"))
                }
                range("pipeline.generation.context_k", pipeline.generation.contextK, 1, 20, this)
            }
        if (errors.isNotEmpty()) throw RequestValidationException(errors)
    }

    private fun range(
        field: String,
        value: Int,
        min: Int,
        max: Int,
        errors: MutableList<FieldError>,
    ) {
        if (value !in min..max) errors += FieldError(field, "must be between $min and $max")
    }
}

@JsonInclude(JsonInclude.Include.ALWAYS)
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
    val usage: Usage = Usage(),
)

data class FilterResult(
    val applied: ExplicitFilters,
    val extracted: ExplicitFilters,
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
)

data class Usage(
    val embedPromptTokens: Int = 0,
    val rerankPairs: Int = 0,
    val llmPromptTokens: Int = 0,
    val llmCompletionTokens: Int = 0,
    val llmCalls: Int = 0,
)

class RetrievalUnavailableException(
    message: String,
    cause: Throwable? = null,
) : RuntimeException(message, cause)
