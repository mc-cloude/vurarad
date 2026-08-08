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

    # WP14 — metering, spend ceiling, and licence services.  The Firestore
    # client is constructed lazily here (not at import) so a misconfigured
    # environment fails at startup, not at module load.
    from google.cloud.firestore import AsyncClient

    from app.billing.rate_card import RateCard
    from app.repositories.usage_repo import FirestoreLicenceStore, UsageRepo
    from app.services.ceiling_service import CeilingService
    from app.services.licence_service import LicenceService
    from app.services.metering_service import MeteringService

    fs_client = AsyncClient(project=settings.gcp_project_id, database=settings.firestore_database)
    usage_repo = UsageRepo(fs_client)
    rate_card = RateCard.load(settings.gcp_region)
    metering = MeteringService(
        usage_repo, flush_interval_seconds=float(settings.metering_flush_interval_seconds)
    )
    app.state.usage_store = usage_repo
    app.state.metering_service = metering
    app.state.ceiling_service = CeilingService(usage_repo, rate_card, settings)
    app.state.licence_service = LicenceService(
        FirestoreLicenceStore(fs_client),
        public_key_pem=settings.licence_public_key_pem,
        grace_days=settings.licence_grace_days,
    )
    await metering.start()
    try:
        yield
    finally:
        # Drain on lifespan shutdown (criterion 2). SIGTERM is handled in
        # MeteringService itself; this covers graceful container stop.
        await metering.stop()

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
    from app.api.v1.routers.health import router as health_router
    from app.api.v1.routers.licence import router as licence_router
    from app.api.v1.routers.usage import router as usage_router

    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(usage_router, prefix="/api/v1")
    app.include_router(licence_router, prefix="/api/v1")

    return app


# -- WSGI/ASGI entrypoint for uvicorn ----------------------------------------
app = create_app()
