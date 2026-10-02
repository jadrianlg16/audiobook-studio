"""Talks to engine microservices over the shared HTTP contract.

The gateway never imports an engine's code — it only knows the contract. This
class fans requests out concurrently and is the single place that touches
``httpx``, which keeps the routers thin and the whole thing easy to test
(swap this class for a fake)."""

from __future__ import annotations

import asyncio
import base64

import httpx

from shared.schemas import EngineInfo, SynthRequest

from ..models import BakeoffItem, BakeoffResult, EngineStatus


class EngineClient:
    def __init__(
        self,
        engines: dict[str, str],
        request_timeout: float = 600.0,
        health_timeout: float = 3.0,
        bakeoff_concurrency: int = 2,
        bakeoff_unload: bool = False,
    ) -> None:
        self._engines = engines
        self._request_timeout = request_timeout
        self._health_timeout = health_timeout
        self._bakeoff_unload = bakeoff_unload
        # Unloading between synths only saves VRAM if the synths are also
        # serialized — two engines loading at once still OOMs (and now thrashes
        # with reload churn on top). So force concurrency=1 whenever unload is on.
        self._bakeoff_concurrency = 1 if bakeoff_unload else max(1, bakeoff_concurrency)

    @property
    def engines(self) -> dict[str, str]:
        return self._engines

    async def _status_one(
        self, client: httpx.AsyncClient, engine_id: str, url: str
    ) -> EngineStatus:
        try:
            info_resp = await client.get(f"{url}/info", timeout=self._health_timeout)
            info_resp.raise_for_status()
            info = EngineInfo.model_validate(info_resp.json())
            health = await client.get(f"{url}/health", timeout=self._health_timeout)
            payload = health.json()
            ready = bool(payload.get("ready"))
            return EngineStatus(
                id=engine_id,
                url=url,
                available=ready,
                loaded=bool(payload.get("loaded")),
                info=info,
                error=None if ready else payload.get("error"),
            )
        except Exception as exc:  # noqa: BLE001 - unreachable engines are normal
            return EngineStatus(
                id=engine_id,
                url=url,
                available=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def list_status(self) -> list[EngineStatus]:
        async with httpx.AsyncClient() as client:
            return list(
                await asyncio.gather(
                    *(self._status_one(client, eid, url) for eid, url in self._engines.items())
                )
            )

    async def synthesize(self, engine_id: str, req: SynthRequest) -> tuple[bytes, dict]:
        url = self._engines.get(engine_id)
        if url is None:
            raise KeyError(engine_id)
        async with httpx.AsyncClient(timeout=self._request_timeout) as client:
            resp = await client.post(f"{url}/synthesize", json=req.model_dump())
            resp.raise_for_status()
            meta = {
                "gen_seconds": _to_float(resp.headers.get("X-Gen-Seconds")),
                "audio_seconds": _to_float(resp.headers.get("X-Audio-Seconds")),
                "rtf": _to_float(resp.headers.get("X-RTF")),
                "sample_rate": int(_to_float(resp.headers.get("X-Sample-Rate"))),
            }
            return resp.content, meta

    async def unload(self, engine_id: str) -> dict:
        url = self._engines.get(engine_id)
        if url is None:
            raise KeyError(engine_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{url}/unload")
            resp.raise_for_status()
            return resp.json()

    async def _bakeoff_one(self, text: str, item: BakeoffItem) -> BakeoffResult:
        req = SynthRequest(
            # A per-item text wins, so one engine can get the tag-annotated
            # passage while the rest get the plain one in the same bake-off.
            text=item.text or text,
            voice=item.voice,
            reference_audio_b64=item.reference_audio_b64,
            reference_text=item.reference_text,
            emotion=item.emotion,
            emotion_label=item.emotion_label,
            emotion_reference_b64=item.emotion_reference_b64,
            instruction=item.instruction,
            speed=item.speed,
            seed=item.seed,
            unload_after=self._bakeoff_unload,
            extra=item.extra,
        )
        try:
            audio, meta = await self.synthesize(item.engine_id, req)
            return BakeoffResult(
                engine_id=item.engine_id,
                ok=True,
                audio_b64=base64.b64encode(audio).decode("ascii"),
                meta=meta,
            )
        except KeyError:
            return BakeoffResult(
                engine_id=item.engine_id, ok=False, error="unknown engine id"
            )
        except httpx.HTTPStatusError as exc:
            return BakeoffResult(
                engine_id=item.engine_id,
                ok=False,
                error=f"HTTP {exc.response.status_code}: {exc.response.text[:300]}",
            )
        except Exception as exc:  # noqa: BLE001
            return BakeoffResult(
                engine_id=item.engine_id, ok=False, error=f"{type(exc).__name__}: {exc}"
            )

    async def bakeoff(self, text: str, items: list[BakeoffItem]) -> list[BakeoffResult]:
        # Cap parallelism so several GPU engines don't load/run at once on one card.
        sem = asyncio.Semaphore(self._bakeoff_concurrency)

        async def guarded(item: BakeoffItem) -> BakeoffResult:
            async with sem:
                return await self._bakeoff_one(text, item)

        return list(await asyncio.gather(*(guarded(it) for it in items)))


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value is not None else 0.0
    except ValueError:
        return 0.0
