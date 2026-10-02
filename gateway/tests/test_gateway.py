"""Gateway tests. The engine client is replaced with an in-memory fake, so these
run with zero network and zero models — they verify the orchestration logic
(discovery, proxy, bake-off fan-out), which is the gateway's whole job.

Run from the project root:  pytest gateway/tests
"""

from __future__ import annotations

import asyncio
import base64

from fastapi.testclient import TestClient

from gateway.app.dependencies import get_engine_client
from gateway.app.main import create_app
from gateway.app.models import BakeoffItem, BakeoffResult, EngineStatus
from gateway.app.services import director as director_service
from gateway.app.services.engine_client import EngineClient
from shared.schemas import Capabilities, EngineInfo, SynthRequest

_FAKE_INFO = EngineInfo(
    id="mock",
    name="Mock",
    description="test engine",
    license="MIT",
    sample_rate=24000,
    capabilities=Capabilities(multi_speaker=True),
)


class FakeEngineClient:
    """Stand-in for EngineClient with deterministic, network-free behavior."""

    def __init__(self) -> None:
        self.synth_calls: list[tuple[str, SynthRequest]] = []

    async def list_status(self) -> list[EngineStatus]:
        return [
            EngineStatus(id="mock", url="http://mock", available=True, info=_FAKE_INFO),
            EngineStatus(id="down", url="http://down", available=False, error="boom"),
        ]

    async def synthesize(self, engine_id: str, req: SynthRequest):
        self.synth_calls.append((engine_id, req))
        if engine_id == "missing":
            raise KeyError(engine_id)
        return b"RIFFfake", {"rtf": 0.12, "gen_seconds": 0.3, "audio_seconds": 2.5, "sample_rate": 24000}

    async def unload(self, engine_id: str) -> dict:
        if engine_id == "missing":
            raise KeyError(engine_id)
        return {"id": engine_id, "loaded": False}

    async def bakeoff(self, text: str, items: list[BakeoffItem]) -> list[BakeoffResult]:
        return [
            BakeoffResult(
                engine_id=it.engine_id,
                ok=True,
                audio_b64=base64.b64encode(f"{it.engine_id}:{text}".encode()).decode(),
                meta={"rtf": 0.1},
            )
            for it in items
        ]


def _client() -> tuple[TestClient, FakeEngineClient]:
    app = create_app()
    fake = FakeEngineClient()
    app.dependency_overrides[get_engine_client] = lambda: fake
    return TestClient(app), fake


def test_health() -> None:
    client, _ = _client()
    assert client.get("/api/health").json() == {"status": "ok"}


def test_list_engines_reports_availability() -> None:
    client, _ = _client()
    body = client.get("/api/engines").json()
    by_id = {e["id"]: e for e in body}
    assert by_id["mock"]["available"] is True
    assert by_id["down"]["available"] is False
    assert by_id["down"]["error"] == "boom"


def test_synthesize_proxies_and_sets_headers() -> None:
    client, fake = _client()
    resp = client.post("/api/synthesize/mock", json={"text": "hello"})
    assert resp.status_code == 200
    assert resp.content == b"RIFFfake"
    assert resp.headers["X-RTF"] == "0.12"
    assert fake.synth_calls[0][0] == "mock"


def test_synthesize_unknown_engine_404() -> None:
    client, _ = _client()
    resp = client.post("/api/synthesize/missing", json={"text": "hi"})
    assert resp.status_code == 404


def test_bakeoff_fans_out_to_all_items() -> None:
    client, _ = _client()
    payload = {
        "text": "Once upon a time",
        "items": [{"engine_id": "mock"}, {"engine_id": "kokoro", "emotion": 0.7}],
    }
    body = client.post("/api/bakeoff", json=payload).json()
    ids = [r["engine_id"] for r in body["results"]]
    assert ids == ["mock", "kokoro"]
    assert all(r["ok"] for r in body["results"])
    decoded = base64.b64decode(body["results"][0]["audio_b64"]).decode()
    assert decoded == "mock:Once upon a time"


def test_unload_endpoint() -> None:
    client, _ = _client()
    resp = client.post("/api/unload/mock")
    assert resp.status_code == 200
    assert resp.json() == {"id": "mock", "loaded": False}


def test_bakeoff_unload_sets_flag_on_requests() -> None:
    """With bakeoff_unload=True every synth request must carry unload_after=True
    so engines free VRAM between turns."""
    client = EngineClient(engines={"a": "u"}, bakeoff_unload=True)
    seen: list[SynthRequest] = []

    async def fake_synth(_engine_id: str, req: SynthRequest):
        seen.append(req)
        return b"RIFF", {"rtf": 0.0}

    client.synthesize = fake_synth  # type: ignore[assignment]
    asyncio.run(client.bakeoff("hi", [BakeoffItem(engine_id="a")]))
    assert seen[0].unload_after is True


def test_bakeoff_unload_forces_serial_execution() -> None:
    """BAKEOFF_UNLOAD only saves VRAM if synths are also serialized, so enabling
    it must pin concurrency to 1 no matter what concurrency was requested."""
    client = EngineClient(
        engines={"a": "u", "b": "u", "c": "u"}, bakeoff_concurrency=4, bakeoff_unload=True
    )
    state = {"current": 0, "peak": 0}

    async def fake_synth(_engine_id: str, _req: SynthRequest):
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.02)
        state["current"] -= 1
        return b"RIFF", {"rtf": 0.0}

    client.synthesize = fake_synth  # type: ignore[assignment]
    items = [BakeoffItem(engine_id=e) for e in ("a", "b", "c")]
    asyncio.run(client.bakeoff("hi", items))
    assert state["peak"] == 1  # unload=True overrode concurrency=4


def test_director_endpoint_maps_value_error_to_400(monkeypatch) -> None:
    """A safety-blocked/empty LLM reply raises ValueError in the service; the
    router must turn that into a clean 400, not a 500 stack trace."""

    async def boom(req, ollama_url, gemini_key):
        raise ValueError("Gemini returned no output (SAFETY)")

    monkeypatch.setattr(director_service, "annotate", boom)
    client = TestClient(create_app())
    resp = client.post(
        "/api/director/annotate",
        json={"text": "hi", "provider": "gemini", "model": "x", "api_key": "k", "engines": []},
    )
    assert resp.status_code == 400
    assert "no output" in resp.json()["detail"]


def test_director_parse_strips_fence_and_normalizes() -> None:
    out = director_service._parse(
        '```json\n{"summary":"x","emotion":"angry","scalar":5,"segments":"nope"}\n```'
    )
    assert out["scalar"] == 1.0  # clamped into [0,1]
    assert out["segments"] == []  # bad type normalized to a list


def test_director_validate_per_engine_coerces_to_allowed() -> None:
    from gateway.app.models import DirectorEngineSpec

    engines = [
        DirectorEngineSpec(id="indextts2", emotion_labels=["angry", "sad", "calm"], voices=[]),
        DirectorEngineSpec(id="kokoro", emotion_labels=[], voices=["af_heart", "am_michael"]),
    ]
    raw = [
        {"engine_id": "indextts2", "emotion_label": "ANGRY", "voice": "nope", "scalar": 2.0},
        {"engine_id": "kokoro", "emotion_label": "happy", "voice": "am_michael", "scalar": 0.3},
        {"engine_id": "ghost", "emotion_label": "x"},  # unknown -> dropped
    ]
    out = {d["engine_id"]: d for d in director_service._validate_per_engine(raw, engines, 0.5)}
    assert set(out) == {"indextts2", "kokoro"}  # ghost dropped; every engine present
    assert out["indextts2"]["emotion_label"] == "angry"  # case-normalized to allowed
    assert out["indextts2"]["voice"] is None  # not an allowed voice
    assert out["indextts2"]["scalar"] == 1.0  # clamped to [0,1]
    assert out["kokoro"]["emotion_label"] is None  # "happy" not in []
    assert out["kokoro"]["voice"] == "am_michael"


def test_director_endpoint(monkeypatch) -> None:
    async def fake_annotate(req, ollama_url, gemini_key):
        return {
            "summary": "tense",
            "emotion": "angry",
            "scalar": 0.8,
            "voice_hint": "cold",
            "segments": [{"speaker": "She", "text": "...", "emotion": "angry"}],
            "per_engine": [
                {"engine_id": "styletts2", "emotion_label": None, "voice": None, "scalar": 0.7}
            ],
        }

    monkeypatch.setattr(director_service, "annotate", fake_annotate)
    client = TestClient(create_app())
    resp = client.post(
        "/api/director/annotate",
        json={
            "text": "hello",
            "provider": "ollama",
            "model": "llama3.2",
            "engines": [{"id": "styletts2", "emotion_scalar": True}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["emotion"] == "angry"
    assert body["scalar"] == 0.8
    assert body["per_engine"][0]["engine_id"] == "styletts2"


def test_bakeoff_respects_concurrency_limit() -> None:
    """With concurrency=1 the bake-off must run engines strictly one at a time —
    this is what protects a single GPU from simultaneous model loads."""
    client = EngineClient(engines={"a": "u", "b": "u", "c": "u"}, bakeoff_concurrency=1)
    state = {"current": 0, "peak": 0}

    async def fake_synth(_engine_id: str, _req: SynthRequest):
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.02)
        state["current"] -= 1
        return b"RIFF", {"rtf": 0.0}

    client.synthesize = fake_synth  # type: ignore[assignment]
    items = [BakeoffItem(engine_id=e) for e in ("a", "b", "c")]
    results = asyncio.run(client.bakeoff("hi", items))

    assert [r.ok for r in results] == [True, True, True]
    assert state["peak"] == 1  # never two engines at once


def test_bakeoff_item_text_overrides_the_shared_text() -> None:
    """The director hands a tag-annotated passage to the one engine that speaks
    tags; every other engine in the same run must still get the plain text, or
    "(sigh)" gets read aloud as three literal words."""
    client = EngineClient(engines={"breeze": "u", "kokoro": "u"})
    seen: dict[str, str] = {}

    async def fake_synth(engine_id: str, req: SynthRequest):
        seen[engine_id] = req.text
        return b"RIFF", {"rtf": 0.0}

    client.synthesize = fake_synth  # type: ignore[assignment]
    items = [
        BakeoffItem(engine_id="breeze", text="(sigh) Hello."),
        BakeoffItem(engine_id="kokoro"),
    ]
    asyncio.run(client.bakeoff("Hello.", items))
    assert seen["breeze"] == "(sigh) Hello."
    assert seen["kokoro"] == "Hello."


def test_bakeoff_forwards_instruction_and_reference_text() -> None:
    """Breeze's two new inputs must survive the gateway hop."""
    client = EngineClient(engines={"breeze": "u"})
    seen: list[SynthRequest] = []

    async def fake_synth(_engine_id: str, req: SynthRequest):
        seen.append(req)
        return b"RIFF", {"rtf": 0.0}

    client.synthesize = fake_synth  # type: ignore[assignment]
    asyncio.run(
        client.bakeoff(
            "Hello.",
            [BakeoffItem(engine_id="breeze", instruction="Slow.", reference_text="A clip.")],
        )
    )
    assert seen[0].instruction == "Slow."
    assert seen[0].reference_text == "A clip."
