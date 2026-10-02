from __future__ import annotations

from fastapi import APIRouter, Depends

from ..dependencies import get_engine_client
from ..models import BakeoffRequest, BakeoffResponse
from ..services.engine_client import EngineClient

router = APIRouter(tags=["bakeoff"])


@router.post("/bakeoff", response_model=BakeoffResponse)
async def bakeoff(
    req: BakeoffRequest,
    client: EngineClient = Depends(get_engine_client),
) -> BakeoffResponse:
    """Synthesize the same text on several engines concurrently and return all
    clips (base64 WAV) plus timing metrics for side-by-side comparison."""
    results = await client.bakeoff(req.text, req.items)
    return BakeoffResponse(results=results)
