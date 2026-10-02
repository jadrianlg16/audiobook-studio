"""Gateway configuration. Engines are discovered from a simple registry that
defaults to the docker-compose service names and can be overridden via the
``ENGINES`` env var (format: ``id=url;id=url``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default registry — matches the service names in docker-compose.yml.
DEFAULT_ENGINES: dict[str, str] = {
    "mock": "http://mock:8000",
    "kokoro": "http://kokoro:8000",
    "chatterbox": "http://chatterbox:8000",
    "styletts2": "http://styletts2:8000",
    "orpheus": "http://orpheus:8000",
    "indextts2": "http://indextts2:8000",
    "breeze": "http://breeze:8000",
    "qwen3tts": "http://qwen3tts:8000",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    engines_raw: str = Field(default="", validation_alias="ENGINES")
    request_timeout: float = 600.0  # synthesis can be slow on big models
    health_timeout: float = 3.0
    cors_origins: str = "*"
    # Max engines synthesized in parallel during a bake-off. Defaults to 1
    # (safe on a single shared GPU: concurrent model loads are what blow up
    # VRAM). Raise it for CPU-only engines or a roomy card.
    bakeoff_concurrency: int = 1
    # Low-VRAM mode: ask each engine to free its model after its bake-off synth.
    # Combine with bakeoff_concurrency=1 so only one model is ever resident — lets
    # several heavy GPU engines bake off on a small card (e.g. an 8 GB 3060).
    bakeoff_unload: bool = False

    # AI director (LLM that reads text and emits emotion/voice direction).
    # Ollama runs on the host; from inside Docker reach it via host.docker.internal.
    ollama_url: str = "http://host.docker.internal:11434"
    gemini_api_key: str = ""  # optional fallback; the UI normally supplies the key
    # Default local model the UI pre-fills for the director. Set DIRECTOR_MODEL in
    # .env per machine — an 8 GB box and a 48 GB box want different directors, and
    # neither should need a code change or a frontend rebuild to switch.
    director_model: str = "llama3.2"

    @property
    def engines(self) -> dict[str, str]:
        if not self.engines_raw.strip():
            return dict(DEFAULT_ENGINES)
        registry: dict[str, str] = {}
        for pair in self.engines_raw.split(";"):
            pair = pair.strip()
            if not pair:
                continue
            engine_id, _, url = pair.partition("=")
            registry[engine_id.strip()] = url.strip()
        return registry


@lru_cache
def get_settings() -> Settings:
    return Settings()
