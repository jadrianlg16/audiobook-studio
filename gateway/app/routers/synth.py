from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from shared.schemas import SynthRequest

from ..dependencies import get_engine_client
from ..services.engine_client import EngineClient

router = APIRouter(tags=["synthesis"])


@router.post("/synthesize/{engine_id}")
async def synthesize(
    engine_id: str,
    req: SynthRequest,
    client: EngineClient = Depends(get_engine_client),
) -> Response:
    """Proxy a single synthesis request to one engine and stream back WAV."""
    try:
        audio, meta = await client.synthesize(engine_id, req)
    except KeyError:
        raise HTTPException(404, f"unknown engine '{engine_id}'")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, exc.response.text)
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"engine unreachable: {exc}")
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "X-Engine-Id": engine_id,
            "X-RTF": str(meta.get("rtf", 0)),
            "X-Gen-Seconds": str(meta.get("gen_seconds", 0)),
            "X-Sample-Rate": str(meta.get("sample_rate", 0)),
        },
    )


@router.post("/unload/{engine_id}")
async def unload(
    engine_id: str,
    client: EngineClient = Depends(get_engine_client),
) -> dict:
    """Free an engine's model from VRAM now (reloads lazily on next synth)."""
    try:
        return await client.unload(engine_id)
    except KeyError:
        raise HTTPException(404, f"unknown engine '{engine_id}'")
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"engine unreachable: {exc}")
