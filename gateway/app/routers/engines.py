from __future__ import annotations

from fastapi import APIRouter, Depends

from ..dependencies import get_engine_client
from ..models import EngineStatus
from ..services.engine_client import EngineClient

router = APIRouter(tags=["engines"])


@router.get("/engines", response_model=list[EngineStatus])
async def list_engines(client: EngineClient = Depends(get_engine_client)) -> list[EngineStatus]:
    """Discover all registered engines and report which are available right now."""
    return await client.list_status()
