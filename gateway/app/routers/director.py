from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ..config import Settings, get_settings
from ..models import DirectorModel, DirectorModels, DirectorRequest, DirectorResult
from ..services import director

router = APIRouter(tags=["director"])


@router.get("/director/defaults")
def defaults(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """The director model this deployment prefers, so the UI doesn't hardcode one.
    Set DIRECTOR_MODEL in .env to move between machines.

    ``prompt_version`` rides along because a book-scale pass caches per scene on
    (passage, model, prompt) — the caller needs the prompt hash before it can
    decide whether a stored pass is still current.
    """
    return {
        "provider": "ollama",
        "model": settings.director_model,
        "prompt_version": director.prompt_version(),
    }


@router.get("/director/models", response_model=DirectorModels)
async def models(settings: Settings = Depends(get_settings)) -> DirectorModels:
    """List the models Ollama has actually pulled.

    Always 200. An unreachable Ollama (not running, wrong host from inside
    Docker, still downloading) returns an empty list with ``error`` set, because
    the UI's job here is to *show* that state next to the model picker — a 503
    would just render as a blank dropdown and lose the reason.
    """
    url = f"{settings.ollama_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=settings.health_timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        return DirectorModels(
            default=settings.director_model,
            error=f"Ollama returned HTTP {exc.response.status_code} for {url}",
        )
    except Exception as exc:  # noqa: BLE001 - unreachable, DNS, bad JSON, timeout
        return DirectorModels(
            default=settings.director_model,
            error=f"could not reach Ollama at {settings.ollama_url}: {type(exc).__name__}: {exc}",
        )

    found: list[DirectorModel] = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        # Ollama's own field is `size`; `name` carries the tag ("gemma3:12b").
        name = str(item.get("name") or item.get("model") or "").strip()
        if not name:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        found.append(
            DirectorModel(name=name, size_bytes=size, modified_at=str(item.get("modified_at") or ""))
        )
    return DirectorModels(models=found, default=settings.director_model, error=None)


@router.post("/director/annotate", response_model=DirectorResult)
async def annotate(
    req: DirectorRequest,
    settings: Settings = Depends(get_settings),
) -> DirectorResult:
    """Have an LLM (Ollama or Gemini) read the text and return emotional
    direction that the UI applies to the engine controls."""
    try:
        data = await director.annotate(req, settings.ollama_url, settings.gemini_api_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"{req.provider} error: {exc.response.text[:300]}")
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"{req.provider} unreachable: {exc}")
    return DirectorResult(**data)
