"""FastAPI dependency providers. Centralized so tests can override the engine
client with a fake via ``app.dependency_overrides``."""

from __future__ import annotations

from .config import get_settings
from .services.engine_client import EngineClient


def get_engine_client() -> EngineClient:
    settings = get_settings()
    return EngineClient(
        engines=settings.engines,
        request_timeout=settings.request_timeout,
        health_timeout=settings.health_timeout,
        bakeoff_concurrency=settings.bakeoff_concurrency,
        bakeoff_unload=settings.bakeoff_unload,
    )
