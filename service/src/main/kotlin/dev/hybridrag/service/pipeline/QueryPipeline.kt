package dev.hybridrag.service.pipeline

import dev.hybridrag.service.api.AnswerResponse
import dev.hybridrag.service.api.ChunkResponse
import dev.hybridrag.service.api.QueryRequest
import dev.hybridrag.service.api.QueryResponse
import dev.hybridrag.service.api.RetrievalUnavailableException
import dev.hybridrag.service.api.SqlResult
import dev.hybridrag.service.api.Usage
import dev.hybridrag.service.generation.AnswerGenerator
import dev.hybridrag.service.generation.GeneratedAnswer
import dev.hybridrag.service.llm.EmbeddingClient
import dev.hybridrag.service.llm.LlmClient
import dev.hybridrag.service.llm.LlmReply
import dev.hybridrag.service.observability.PropagatingExecutor
import dev.hybridrag.service.observability.Stage
import dev.hybridrag.service.observability.TraceContext
import dev.hybridrag.service.rerank.RerankPassage
import dev.hybridrag.service.rerank.RerankerClient
import dev.hybridrag.service.resilience.Dependency
import dev.hybridrag.service.resilience.DependencyGuards
import dev.hybridrag.service.retrieval.Candidate
import dev.hybridrag.service.retrieval.ChunkHydrator
import dev.hybridrag.service.retrieval.FilterPlan
import dev.hybridrag.service.retrieval.FilterPlanner
import dev.hybridrag.service.retrieval.LexicalRetriever
import dev.hybridrag.service.retrieval.RerankMerge
import dev.hybridrag.service.retrieval.RrfFusion
import dev.hybridrag.service.retrieval.VectorResult
import dev.hybridrag.service.retrieval.VectorRetriever
import dev.hybridrag.service.routing.GuardRejectedException
import dev.hybridrag.service.routing.RouteClassifier
import dev.hybridrag.service.routing.SqlExecutionException
import dev.hybridrag.service.routing.SqlExecutor
import dev.hybridrag.service.routing.SqlGenerator
import dev.hybridrag.service.routing.SqlGuard
import io.opentelemetry.api.trace.Span
import io.opentelemetry.api.trace.Tracer
import net.logstash.logback.argument.StructuredArguments.kv
import org.slf4j.LoggerFactory
import org.springframework.stereotype.Service
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutionException
import java.util.concurrent.Future
import java.util.concurrent.atomic.AtomicInteger

/** Request flow of design D2: route → (SQL path) → filter.estimate → parallel legs → fuse → rerank → generate. */
@Service
class QueryPipeline(
    private val tracer: Tracer,
    private val routeClassifier: RouteClassifier,
    private val sqlGenerator: SqlGenerator,
    private val sqlGuard: SqlGuard,
    private val sqlExecutor: SqlExecutor,
    private val filterPlanner: FilterPlanner,
    private val embeddingClient: EmbeddingClient,
    private val vectorRetriever: VectorRetriever,
    private val lexicalRetriever: LexicalRetriever,
    private val fusion: RrfFusion,
    private val hydrator: ChunkHydrator,
    private val reranker: RerankerClient,
    private val answers: AnswerGenerator,
    private val llm: LlmClient,
    private val guards: DependencyGuards,
) {
    private val logger = LoggerFactory.getLogger(javaClass)

    fun query(request: QueryRequest): QueryResponse {
        val run = Run(request, Stage(tracer, ConcurrentHashMap()))
        val p = request.pipeline
        var route = "vector"
        var routeSource = if (p.routing.enabled) "llm" else "disabled"
        var sql: SqlResult? = null

        if (p.routing.enabled) {
            run.stage
                .run("route") { span ->
                    routeClassifier.classify(request.question).also { span.setAttribute("rag.route", it.route) }
                }.onSuccess {
                    route = it.route
                    run.llm(it.reply)
                }.onFailure {
                    routeSource = "fallback"
                    run.degrade("router_unavailable", Dependency.LLM.id, it)
                }
        }

        if (route == "sql") {
            val outcome = sqlPath(run)
            if (outcome.error == null) {
                val answer =
                    if (p.generation.enabled) {
                        generate(
                            run,
                        ) { answers.fromSql(request.question, outcome) }
                    } else {
                        AnswerResponse.DISABLED
                    }
                return finish(run, "sql", routeSource, null, emptyList(), outcome, answer)
            }
            sql = outcome
            route = "vector"
            routeSource = "fallback"
            run.degrade("sql_fallback", "sql", RuntimeException(outcome.error))
        }

        var plan =
            if (p.filters.enabled) {
                run.stage
                    .run("filter.estimate") { span ->
                        filterPlanner.plan(request.question, p.filters, p.retrieval.vector).also {
                            span.setAttribute("rag.filter.strategy", it.strategy)
                            it.selectivity?.let { value -> span.setAttribute("rag.filter.selectivity", value) }
                        }
                    }.getOrElse { throw unavailable("filter estimate failed", it) }
            } else {
                null
            }

        val (vector, lexical) = retrieve(run, plan)
        if (plan != null && vector != null && plan.strategy == "post") plan = plan.copy(underfilled = vector.underfilled)

        val fused =
            if (vector != null && lexical != null) {
                run.stage.run("fuse") { fusion.fuse(vector.candidates, lexical, p.retrieval.fusion.k, p.retrieval.returnK) }.getOrThrow()
            } else {
                fusion.single(vector?.candidates ?: lexical.orEmpty(), p.retrieval.returnK)
            }
        val hydrated = runCatching { hydrator.hydrate(fused) }.getOrElse { throw unavailable("chunk hydration failed", it) }
        val chunks = hydrator.reorder(hydrated, rerank(run, fused, hydrated))

        val answer =
            if (p.generation.enabled) {
                generate(run) { answers.fromChunks(request.question, chunks.take(p.generation.contextK)) }
            } else {
                AnswerResponse.DISABLED
            }
        return finish(run, route, routeSource, plan, chunks, sql, answer)
    }

    /** Any failure in the SQL path yields a `sql` object with an error; the caller falls back to the vector route. */
    private fun sqlPath(run: Run): SqlResult {
        val question = run.request.question
        val generated =
            run.stage.run("sql.generate") { sqlGenerator.generate(question) }.getOrElse {
                return failedSql(null, "generation_failed: ${it.message}")
            }
        run.llm(generated.reply)
        val checked =
            try {
                sqlGuard.check(generated.sql)
            } catch (error: GuardRejectedException) {
                return failedSql(generated.sql, "guard_rejected: ${error.message}")
            }
        return run.stage.run("sql.execute") { sqlExecutor.execute(checked) }.getOrElse {
            failedSql(checked, (it as? SqlExecutionException)?.message ?: "execution_failed: ${it.message}")
        }
    }

    private fun failedSql(
        query: String?,
        error: String,
    ) = SqlResult(query, emptyList(), emptyList(), 0, false, error)

    /** Leg A (`embed` → `retrieve.vector`) and leg B (`retrieve.bm25`) in parallel on virtual threads. */
    private fun retrieve(
        run: Run,
        plan: FilterPlan?,
    ): Pair<VectorResult?, List<Candidate>?> {
        val p = run.request.pipeline
        PropagatingExecutor().use { executor ->
            val vector = if (p.retrieval.vector) executor.submit { vectorLeg(run, plan) } else null
            val lexical =
                if (p.retrieval.bm25) {
                    executor.submit {
                        run.stage
                            .run("retrieve.bm25") { span ->
                                lexicalRetriever.retrieve(run.request.question, p.contextual, p.retrieval.bm25K, plan).also {
                                    span.setAttribute("rag.contextual", p.contextual)
                                    span.setAttribute("rag.candidates", it.size.toLong())
                                }
                            }.getOrElse { throw unavailable("lexical retrieval failed", it) }
                    }
                } else {
                    null
                }
            return await(vector) to await(lexical)
        }
    }

    private fun vectorLeg(
        run: Run,
        plan: FilterPlan?,
    ): VectorResult? {
        val p = run.request.pipeline
        val embedding =
            run.stage.run("embed") { embeddingClient.embed(run.request.question) }.getOrElse {
                if (!p.retrieval.bm25) throw unavailable("embedding unavailable and the bm25 leg is disabled", it)
                run.degrade("embedding_unavailable", Dependency.EMBEDDING.id, it)
                return null
            }
        run.embedTokens.addAndGet(embedding.promptTokens)
        return run.stage
            .run("retrieve.vector") { span ->
                vectorRetriever.retrieve(embedding.vector, p.contextual, p.retrieval.vectorK, p.filters.overfetchFactor, plan).also {
                    span.setAttribute("rag.contextual", p.contextual)
                    span.setAttribute("rag.candidates", it.candidates.size.toLong())
                }
            }.getOrElse { throw unavailable("vector retrieval failed", it) }
    }

    /** Top `candidates` → reranker → merge (design D5); on failure the fused order is kept. */
    private fun rerank(
        run: Run,
        fused: List<Candidate>,
        hydrated: List<ChunkResponse>,
    ): List<Candidate> {
        val p = run.request.pipeline
        if (!p.rerank.enabled || hydrated.isEmpty()) return fused
        val head = hydrated.take(p.rerank.candidates)
        val passages = head.map { RerankPassage(it.chunkId, if (p.contextual) it.contextHeader + "\n\n" + it.content else it.content) }
        return run.stage
            .run("rerank") { span ->
                span.setAttribute("rag.rerank.pairs", passages.size.toLong())
                reranker.rerank(run.request.question, passages)
            }.fold(
                onSuccess = { scores ->
                    run.rerankPairs.addAndGet(passages.size)
                    RerankMerge.merge(fused, scores, p.rerank.candidates)
                },
                onFailure = {
                    run.degrade("rerank_unavailable", Dependency.RERANKER.id, it)
                    fused
                },
            )
    }

    private fun generate(
        run: Run,
        call: () -> GeneratedAnswer,
    ): AnswerResponse =
        run.stage
            .run("generate") { span ->
                span.setAttribute("gen_ai.request.model", llm.model)
                call().also {
                    span.setAttribute("gen_ai.usage.input_tokens", it.reply.promptTokens.toLong())
                    span.setAttribute("gen_ai.usage.output_tokens", it.reply.completionTokens.toLong())
                }
            }.fold(
                onSuccess = {
                    run.llm(it.reply)
                    it.answer
                },
                onFailure = {
                    run.degrade("llm_unavailable", Dependency.LLM.id, it)
                    AnswerGenerator.failure(it)
                },
            )

    private fun finish(
        run: Run,
        route: String,
        routeSource: String,
        plan: FilterPlan?,
        chunks: List<ChunkResponse>,
        sql: SqlResult?,
        answer: AnswerResponse,
    ): QueryResponse {
        run.timings["total"] = (System.nanoTime() - run.started) / 1_000_000.0
        val degraded = synchronized(run.degraded) { run.degraded.toList() }
        val response =
            QueryResponse(
                requestId = TraceContext.requestId(),
                traceId = TraceContext.current(),
                route = route,
                routeSource = routeSource,
                pipeline = run.request.pipeline,
                filters = plan?.response(),
                chunks = chunks,
                sql = sql,
                answer = answer,
                degraded = degraded,
                timingsMs = run.timings.toSortedMap(),
                usage = run.usage(),
            )
        Span.current().apply {
            setAttribute("rag.route", route)
            setAttribute("rag.route_source", routeSource)
            setAttribute("rag.degraded", degraded.joinToString(","))
        }
        logger.info(
            "query_completed",
            kv("event", "query_completed"),
            kv("route", route),
            kv("route_source", routeSource),
            kv("degraded", degraded),
            kv("filter_strategy", plan?.strategy),
            kv("chunks", chunks.size),
            kv("answer_status", answer.status),
            kv("timings_ms", response.timingsMs),
        )
        return response
    }

    private fun unavailable(
        message: String,
        cause: Throwable,
    ): RetrievalUnavailableException =
        cause as? RetrievalUnavailableException ?: RetrievalUnavailableException("$message: ${cause.message}", cause)

    private fun <T> await(future: Future<T>?): T? =
        try {
            future?.get()
        } catch (error: ExecutionException) {
            throw error.cause ?: error
        }

    /** Per-request mutable state; legs run in parallel, so collections and counters are thread-safe. */
    private inner class Run(
        val request: QueryRequest,
        val stage: Stage,
    ) {
        val started = System.nanoTime()
        val timings: MutableMap<String, Double> get() = stage.timings
        val degraded = LinkedHashSet<String>()
        val embedTokens = AtomicInteger()
        val rerankPairs = AtomicInteger()
        private val llmPrompt = AtomicInteger()
        private val llmCompletion = AtomicInteger()
        private val llmCalls = AtomicInteger()

        fun llm(reply: LlmReply) {
            llmCalls.incrementAndGet()
            llmPrompt.addAndGet(reply.promptTokens)
            llmCompletion.addAndGet(reply.completionTokens)
        }

        fun usage() = Usage(embedTokens.get(), rerankPairs.get(), llmPrompt.get(), llmCompletion.get(), llmCalls.get())

        fun degrade(
            code: String,
            dependency: String,
            cause: Throwable,
        ) {
            synchronized(degraded) { degraded += code }
            val breaker = Dependency.entries.firstOrNull { it.id == dependency }?.let { guards.breaker(it).state.name }
            logger.warn(
                "dependency_degraded",
                kv("event", "dependency_degraded"),
                kv("code", code),
                kv("dependency", dependency),
                kv("reason", cause.message ?: cause.javaClass.simpleName),
                kv("breaker_state", breaker),
            )
        }
    }
}
