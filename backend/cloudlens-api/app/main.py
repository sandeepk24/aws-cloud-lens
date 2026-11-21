"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import api_router
from app.core.config import get_settings
from app.core.metrics import LatencyMiddleware, get_latency_recorder


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Startup / shutdown hooks (DB init, connector warm-up) go here later.
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="AWS CloudLens API",
        description=(
            "Inventory, connectors, and analytics for AWS environments. "
            "This scaffold exposes health only; AWS API calls are not enabled yet."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    # The API is read-only and the UI does not send cookies or auth headers:
    # no credentials, GET only. A wildcard origin is never combined with
    # credentials (Starlette would reflect any Origin in that case).
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=[],
    )
    application.add_middleware(LatencyMiddleware, recorder=get_latency_recorder())
    application.include_router(api_router)
    return application


app = create_app()
