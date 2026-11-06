"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app import __version__
from app.api.errors import install_error_handlers
from app.api.routes import approvals, system, workflows
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.services.container import Container

logger = get_logger("http")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """Build the app. The container is created at startup unless one is supplied."""
    settings = settings or (container.settings if container else get_settings())
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = container or Container.build(settings)
        await active.startup()
        app.state.container = active
        try:
            yield
        finally:
            await active.shutdown()

    app = FastAPI(
        title="Relay",
        version=__version__,
        description=(
            "Multi-agent workflow orchestration platform. "
            "From request to execution, intelligently orchestrated.\n\n"
            "Changing actions pause for human approval. Approve or reject them with "
            "`POST /api/v1/approvals/{id}/decision` and an `X-Approver-Token` header."
        ),
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(system.router)
    app.include_router(workflows.router)
    app.include_router(approvals.router)

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        logger.info(
            "http.request",
            extra={
                "fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            },
        )
        return response

    return app


app = create_app()
