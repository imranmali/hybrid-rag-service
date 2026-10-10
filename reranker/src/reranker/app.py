"""FastAPI application for bounded-concurrency cross-encoder reranking."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

import anyio
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse

from reranker.config import Settings, get_settings
from reranker.logging import configure_logging
from reranker.models import HealthResponse, RerankRequest, RerankResponse, RerankResult
from reranker.scorer import CrossEncoderScorer, FakeScorer, Scorer

LOGGER = configure_logging()


def _make_scorer(settings: Settings) -> Scorer:
    if settings.model == "fake":
        return FakeScorer()
    return CrossEncoderScorer(
        model=settings.model,
        revision=settings.model_revision,
        max_length=settings.max_length,
        batch_size=settings.batch_size,
        torch_threads=settings.torch_threads,
    )


def create_app(settings: Settings | None = None, scorer: Scorer | None = None) -> FastAPI:
    """Build an app instance; injection points make lifecycle behavior testable."""

    active_settings = settings or get_settings()
    active_scorer = scorer or _make_scorer(active_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = False
        app.state.load_error = None

        async def load_model() -> None:
            started = perf_counter()
            try:
                await anyio.to_thread.run_sync(active_scorer.load)
            except Exception as error:  # readiness must remain false after model-load failure
                app.state.load_error = error
                LOGGER.exception("model load failed", extra={"event": "model_load_failed"})
            else:
                app.state.ready = True
                LOGGER.info(
                    "model loaded",
                    extra={
                        "event": "model_loaded",
                        "model": active_scorer.model,
                        "load_seconds": round(perf_counter() - started, 3),
                    },
                )

        app.state.load_task = asyncio.create_task(load_model())
        try:
            yield
        finally:
            if not app.state.load_task.done():
                app.state.load_task.cancel()

    app = FastAPI(title="Hybrid RAG reranker", version="0.1.0", lifespan=lifespan)
    app.state.settings = active_settings
    app.state.scorer = active_scorer
    app.state.ready = False
    app.state.semaphore = asyncio.Semaphore(active_settings.max_concurrency)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse | JSONResponse:
        body = HealthResponse(
            status="ok" if app.state.ready else "unavailable",
            model=active_scorer.model,
            revision=active_settings.model_revision,
            max_length=active_settings.max_length,
            ready=app.state.ready,
        )
        if not app.state.ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=body.model_dump(),
            )
        return body

    @app.post("/rerank", response_model=RerankResponse)
    async def rerank(request: RerankRequest, response: Response) -> RerankResponse:
        if len(request.passages) > active_settings.max_passages:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"passages may contain at most {active_settings.max_passages} items",
            )
        if not app.state.ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="model not ready",
            )

        queued_at = perf_counter()
        async with app.state.semaphore:
            inference_started = perf_counter()
            queue_ms = (inference_started - queued_at) * 1_000
            try:
                passage_texts = [passage.text for passage in request.passages]
                scores = await anyio.to_thread.run_sync(
                    active_scorer.score, request.query, passage_texts
                )
            except Exception as error:
                LOGGER.exception("rerank inference failed", extra={"event": "rerank_failed"})
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR) from error
            inference_ms = (perf_counter() - inference_started) * 1_000

        latency_ms = queue_ms + inference_ms
        results = [
            RerankResult(id=passage.id, index=index, score=score)
            for index, (passage, score) in enumerate(zip(request.passages, scores, strict=True))
        ]
        results.sort(key=lambda item: (-item.score, item.index))
        result = RerankResponse(
            model=active_scorer.model,
            results=results[: request.resolved_top_k()],
            latency_ms=latency_ms,
        )
        response.headers["X-Rerank-Latency-Ms"] = str(result.latency_ms)
        LOGGER.info(
            "rerank completed",
            extra={
                "event": "rerank",
                "n_passages": len(request.passages),
                "top_k": request.resolved_top_k(),
                "queue_ms": round(queue_ms, 3),
                "inference_ms": round(inference_ms, 3),
                "latency_ms": round(latency_ms, 3),
                "model": active_scorer.model,
            },
        )
        return result

    return app


app = create_app()
