package dev.hybridrag.service.pipeline

import dev.hybridrag.service.api.AnswerResponse
import dev.hybridrag.service.api.ChunkResponse
import dev.hybridrag.service.api.Pipeline
import dev.hybridrag.service.api.QueryRequest
import dev.hybridrag.service.api.QueryResponse
import dev.hybridrag.service.api.QueryValidator
import dev.hybridrag.service.api.RetrievalUnavailableException
import dev.hybridrag.service.api.SqlResult
import dev.hybridrag.service.api.Usage
import dev.hybridrag.service.llm.OllamaClient
import dev.hybridrag.service.llm.RerankerClient
import dev.hybridrag.service.observability.Stage
import dev.hybridrag.service.observability.TraceContext
import dev.hybridrag.service.retrieval.AppliedFilters
import dev.hybridrag.service.retrieval.Candidate
import dev.hybridrag.service.retrieval.ChunkHydrator
import dev.hybridrag.service.retrieval.FilterExtractor
import dev.hybridrag.service.retrieval.FilterPlanner
import dev.hybridrag.service.retrieval.LexicalRetriever
import dev.hybridrag.service.retrieval.RrfFusion
import dev.hybridrag.service.retrieval.VectorRetriever
import dev.hybridrag.service.routing.RouteClassifier
import dev.hybridrag.service.routing.SqlExecutor
import dev.hybridrag.service.routing.SqlGenerator
import dev.hybridrag.service.routing.SqlGuard
import io.opentelemetry.api.trace.Tracer
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

@Service
class QueryPipeline(
    private val tracer: Tracer,
    private val filterExtractor: FilterExtractor,
    private val filterPlanner: FilterPlanner,
    private val vectorRetriever: VectorRetriever,
    private val lexicalRetriever: LexicalRetriever,
    private val fusion: RrfFusion,
    private val hydrator: ChunkHydrator,
    private val reranker: RerankerClient,
    private val ollama: OllamaClient,
    private val routeClassifier: RouteClassifier,
    private val sqlGenerator: SqlGenerator,
    private val sqlGuard: SqlGuard,
    private val sqlExecutor: SqlExecutor,
) {
    private val logger = LoggerFactory.getLogger(javaClass)

    fun query(request: QueryRequest): QueryResponse {
        QueryValidator.validate(request)
        val started = System.nanoTime()
        val timings = ConcurrentHashMap<String, Double>()
        val stage = Stage(tracer, timings)
        val pipeline = request.pipeline
        val degraded = mutableListOf<String>()
        val usage = UsageAccumulator()
        var route = "vector"
        var routeSource = if (pipeline.routing.enabled) "llm" else "disabled"
        var sql: SqlResult? = null

        if (pipeline.routing.enabled) {
            try {
                route = stage.run("route") { routeClassifier.classify(request.question).route }
            } catch (error: Exception) {
                routeSource = "fallback"
                degraded += "router_unavailable"
                logDegraded("llm", "route classification failed")
            }
        }

        if (route == "sql") {
            val generated =
                try {
                    stage.run("sql.generate") { sqlGenerator.generate(request.question) }
                } catch (error: Exception) {
                    fallbackSql(error, degraded).also {
                        route = "vector"
                        routeSource = "fallback"
                    }
                    null
                }
            if (generated != null) {
                val guarded =
                    try {
                        sqlGuard.validate(generated)
                    } catch (error: Exception) {
                        sql = SqlResult(generated, emptyList(), emptyList(), 0, false, "guard_rejected: ${error.message}")
                        null
                    }
                if (guarded != null) {
                    sql = stage.run("sql.execute") { sqlExecutor.execute(guarded) }
                    if (sql?.error == null) {
                        val answer = answerForSql(request.question, pipeline, sql!!, stage, degraded, usage)
                        return finish(
                            request,
                            pipeline,
                            "sql",
                            routeSource,
                            null,
                            emptyList(),
                            sql,
                            answer,
                            degraded,
                            timings,
                            usage,
                            started,
                        )
                    }
                }
                route = "vector"
                routeSource = "fallback"
                degraded += "sql_fallback"
                logDegraded("router", "SQL path failed")
            }
        }

        var filters =
            if (pipeline.filters.enabled) {
                stage.run("filter.estimate") {
                    filterPlanner.plan(request.question, pipeline.filters, filterExtractor)
                }
            } else {
                null
            }
        if (filters != null && !pipeline.retrieval.vector) {
            filters = filters.copy(strategy = "none", selectivity = null, matching = null)
        }

        val vectorAndLexical = retrieve(request.question, pipeline, filters, stage, degraded, usage)
        val vector = vectorAndLexical.first
        val lexical = vectorAndLexical.second
        if (filters?.strategy == "post" && vector != null) {
            filters = filters.copy(underfilled = vector.size < pipeline.retrieval.vectorK)
        }
        if (vector == null && lexical == null) {
            throw RetrievalUnavailableException("all retrieval legs failed")
        }
        val candidates =
            when {
                vector != null && lexical != null ->
                    stage.run("fuse") {
                        fusion.fuse(vector, lexical, pipeline.retrieval.fusion.k, pipeline.retrieval.returnK)
                    }
                vector != null -> vector.take(pipeline.retrieval.returnK)
                else -> lexical!!.take(pipeline.retrieval.returnK)
            }.toMutableList()

        if (pipeline.rerank.enabled && candidates.isNotEmpty()) {
            try {
                stage.run("rerank") {
                    val considered = candidates.take(pipeline.rerank.candidates)
                    val hydrated = hydrator.hydrate(considered)
                    val scores =
                        reranker.rerank(
                            request.question,
                            hydrated.map {
                                it.chunkId to if (pipeline.contextual) "${it.contextHeader}\n\n${it.content}" else it.content
                            },
                        )
                    usage.rerankPairs += considered.size
                    val indexed = considered.associateBy(Candidate::id)
                    scores.forEach { indexed[it.id]?.rerank = it.score }
                    val tail = candidates.drop(considered.size)
                    candidates.clear()
                    candidates += considered.sortedWith(compareByDescending<Candidate> { it.rerank }.thenBy { it.id })
                    candidates += tail
                }
            } catch (error: Exception) {
                degraded += "rerank_unavailable"
                logDegraded("reranker", error.message ?: "unavailable")
            }
        }

        val chunks = hydrator.hydrate(candidates)
        val answer = answerForChunks(request.question, pipeline, chunks, stage, degraded, usage)
        return finish(
            request,
            pipeline,
            route,
            routeSource,
            filters,
            chunks,
            sql,
            answer,
            degraded,
            timings,
            usage,
            started,
        )
    }

    private fun retrieve(
        question: String,
        pipeline: Pipeline,
        filters: AppliedFilters?,
        stage: Stage,
        degraded: MutableList<String>,
        usage: UsageAccumulator,
    ): Pair<List<Candidate>?, List<Candidate>?> {
        val executor = Executors.newVirtualThreadPerTaskExecutor()
        try {
            val vector =
                if (pipeline.retrieval.vector) {
                    executor.submit(
                        Callable {
                            try {
                                val embedding = stage.run("embed") { ollama.embed(question) }
                                usage.embedPromptTokens += embedding.promptTokens
                                val result = stage.run("retrieve.vector") { vectorRetriever.retrieve(embedding.vector, pipeline, filters) }
                                result.first
                            } catch (error: Exception) {
                                if (!pipeline.retrieval.bm25) throw RetrievalUnavailableException("embedding retrieval unavailable", error)
                                degraded += "embedding_unavailable"
                                logDegraded("embedding", error.message ?: "unavailable")
                                null
                            }
                        },
                    )
                } else {
                    null
                }
            val lexical =
                if (pipeline.retrieval.bm25) {
                    executor.submit(
                        Callable {
                            try {
                                stage.run("retrieve.bm25") { lexicalRetriever.retrieve(question, pipeline, filters) }
                            } catch (error: Exception) {
                                if (!pipeline.retrieval.vector) throw RetrievalUnavailableException("lexical retrieval unavailable", error)
                                null
                            }
                        },
                    )
                } else {
                    null
                }
            return vector?.get() to lexical?.get()
        } finally {
            executor.close()
        }
    }

    private fun answerForChunks(
        question: String,
        pipeline: Pipeline,
        chunks: List<ChunkResponse>,
        stage: Stage,
        degraded: MutableList<String>,
        usage: UsageAccumulator,
    ): AnswerResponse {
        if (!pipeline.generation.enabled) return disabledAnswer()
        return try {
            val passages =
                chunks
                    .take(pipeline.generation.contextK)
                    .mapIndexed { index, chunk ->
                        "[${index + 1}] ${chunk.title} — ${chunk.sectionPath}\n${chunk.content}"
                    }.joinToString("\n\n")
            val system =
                "You answer questions about arXiv papers using ONLY the numbered passages. " +
                    "Cite every claim with the passage number in square brackets, e.g. [2]. If the passages do not " +
                    "contain the answer, reply exactly: I cannot answer this from the indexed papers."
            val reply = stage.run("generate") { ollama.chat(system, "Passages:\n$passages\n\nQuestion: $question", false, 384, 45000) }
            usage.llmCalls += 1
            usage.llmPromptTokens += reply.promptTokens
            usage.llmCompletionTokens += reply.completionTokens
            answer(reply.content, chunks.take(pipeline.generation.contextK))
        } catch (error: Exception) {
            degraded += "llm_unavailable"
            logDegraded("llm", error.message ?: "generation unavailable")
            unavailableAnswer()
        }
    }

    private fun answerForSql(
        question: String,
        pipeline: Pipeline,
        sql: SqlResult,
        stage: Stage,
        degraded: MutableList<String>,
        usage: UsageAccumulator,
    ): AnswerResponse {
        if (!pipeline.generation.enabled) return disabledAnswer()
        return try {
            val reply =
                stage.run("generate") {
                    ollama.chat(
                        "sqlanswer-v1",
                        "Question: $question\nSQL: ${sql.query}\nColumns: ${sql.columns}\nRows: ${sql.rows}\nAnswer concisely using only these rows.",
                        false,
                        384,
                        45000,
                    )
                }
            usage.llmCalls += 1
            usage.llmPromptTokens += reply.promptTokens
            usage.llmCompletionTokens += reply.completionTokens
            AnswerResponse("ok", 200, reply.content, false, emptyList(), null)
        } catch (error: Exception) {
            degraded += "llm_unavailable"
            unavailableAnswer()
        }
    }

    private fun answer(
        text: String,
        chunks: List<ChunkResponse>,
    ): AnswerResponse {
        val citations =
            Regex("\\[(\\d+)]")
                .findAll(text)
                .mapNotNull {
                    chunks.getOrNull(it.groupValues[1].toInt() - 1)?.chunkId
                }.distinct()
                .toList()
        return AnswerResponse(
            "ok",
            200,
            text,
            text.trim().startsWith("I cannot answer this from the indexed papers."),
            citations,
            null,
        )
    }

    private fun fallbackSql(
        error: Exception,
        degraded: MutableList<String>,
    ) {
        degraded += "router_unavailable"
        degraded += "sql_fallback"
        logDegraded("llm", error.message ?: "SQL generation unavailable")
    }

    private fun disabledAnswer() = AnswerResponse("disabled", null, null, false, null, null)

    private fun unavailableAnswer() = AnswerResponse("unavailable", 503, null, false, null, "llm unavailable")

    private fun finish(
        request: QueryRequest,
        pipeline: Pipeline,
        route: String,
        routeSource: String,
        filters: AppliedFilters?,
        chunks: List<ChunkResponse>,
        sql: SqlResult?,
        answer: AnswerResponse,
        degraded: List<String>,
        timings: MutableMap<String, Double>,
        usage: UsageAccumulator,
        started: Long,
    ): QueryResponse {
        timings["total"] = (System.nanoTime() - started) / 1_000_000.0
        val response =
            QueryResponse(
                UUID.randomUUID().toString(),
                TraceContext.get(),
                route,
                routeSource,
                pipeline,
                filters?.response(),
                chunks,
                sql,
                answer,
                degraded.distinct(),
                timings,
                usage.toResponse(),
            )
        logger.info(
            "query_completed request_id={} trace_id={} route={} route_source={} degraded={} chunks={} answer_status={} timings={}",
            response.requestId,
            response.traceId,
            response.route,
            response.routeSource,
            response.degraded,
            response.chunks.size,
            response.answer.status,
            response.timingsMs,
        )
        return response
    }

    private fun logDegraded(
        dependency: String,
        reason: String,
    ) {
        logger.warn("dependency_degraded dependency={} reason={} trace_id={}", dependency, reason, TraceContext.get())
    }

    private class UsageAccumulator {
        var embedPromptTokens = 0
        var rerankPairs = 0
        var llmPromptTokens = 0
        var llmCompletionTokens = 0
        var llmCalls = 0

        fun toResponse() = Usage(embedPromptTokens, rerankPairs, llmPromptTokens, llmCompletionTokens, llmCalls)
    }
}
