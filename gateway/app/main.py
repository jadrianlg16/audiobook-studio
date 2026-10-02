"""Audiobook Studio gateway: discovers engines, proxies synthesis, runs
bake-offs. Engine-agnostic — it only speaks the shared HTTP contract."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import bakeoff, director, engines, synth


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Audiobook Studio Gateway", version="0.1.0")

    origins = (
        ["*"]
        if settings.cors_origins.strip() == "*"
        else [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-RTF", "X-Gen-Seconds", "X-Sample-Rate", "X-Engine-Id"],
    )

    app.include_router(engines.router, prefix="/api")
    app.include_router(synth.router, prefix="/api")
    app.include_router(bakeoff.router, prefix="/api")
    app.include_router(director.router, prefix="/api")

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
