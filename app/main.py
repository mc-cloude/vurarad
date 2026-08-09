"""vuraRAD API — application factory.

create_app() returns a fully wired FastAPI application with middleware,
exception handlers, and routers registered.  Settings are validated at
import time via Settings() — a misconfiguration is an import error, not
a runtime 500.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.errors import register_handlers
from app.core.logging import setup_logging
from app.core.security import SecurityHeadersMiddleware

logger = logging.getLogger("vurarad")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown — validate connectivity, initialise services."""
    logger.info("vuraRAD API starting", extra={"environment": settings.environment.value})
    yield
    logger.info("vuraRAD API shutting down")


def create_app() -> FastAPI:
    setup_logging(level=settings.log_level.value)

    app = FastAPI(
        title="vuraRAD",
        version="0.1.0",
        lifespan=lifespan,
        # Never expose docs in production
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # -- settings on app.state for DI ---------------------------------------
    app.state.settings = settings

    # -- CORS -----------------------------------------------------------------
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Request-Id",
                "Idempotency-Key",
            ],
            allow_credentials=False,  # NEVER True — bearer auth, not cookies
            expose_headers=["X-Request-Id", "Retry-After"],
        )

    # -- security headers ----------------------------------------------------
    app.add_middleware(SecurityHeadersMiddleware)

    # -- exception handlers --------------------------------------------------
    register_handlers(app)

    # -- routers -------------------------------------------------------------
    from app.api.v1.routers.auth import router as auth_router
    from app.api.v1.routers.dictation import router as dictation_router
    from app.api.v1.routers.findings import router as findings_router
    from app.api.v1.routers.health import router as health_router
    from app.api.v1.routers.ingest import router as ingest_router
    from app.api.v1.routers.preprocessing import router as preprocessing_router
    from app.api.v1.routers.studies import router as studies_router
    from app.api.v1.routers.uploads import router as uploads_router
    from app.dicomweb.router import router as dicomweb_router

    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(uploads_router, prefix="/api/v1")
    app.include_router(ingest_router, prefix="/api/v1")
    app.include_router(studies_router, prefix="/api/v1")
    app.include_router(findings_router, prefix="/api/v1")
    app.include_router(dictation_router, prefix="/api/v1")
    app.include_router(preprocessing_router, prefix="/api/v1")
    app.include_router(dicomweb_router)

    return app


# -- WSGI/ASGI entrypoint for uvicorn ----------------------------------------
app = create_app()
