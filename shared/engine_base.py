"""Engine-side helpers: the ``TTSEngine`` base class and a FastAPI app factory.

An engine author implements three methods (``info``, ``_load``, ``synthesize``)
and calls ``create_engine_app(engine)``. They get ``/health``, ``/info`` and
``/synthesize`` for free, with consistent WAV encoding and timing headers.
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
import threading
import time
import wave
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from .schemas import EngineInfo, SynthRequest


class TTSEngine(ABC):
    """Base class for every voice engine. Keep heavy imports inside ``_load``
    so the container can boot and report health even before weights exist."""

    def __init__(self) -> None:
        self._ready = False
        self._load_error: str | None = None

    @abstractmethod
    def info(self) -> EngineInfo:
        """Static metadata. Must be cheap; called before ``load``."""

    @abstractmethod
    def _load(self) -> None:
        """Load model weights into memory. Raise on failure."""

    @abstractmethod
    def synthesize(self, req: SynthRequest) -> np.ndarray:
        """Return mono float32 audio in [-1, 1] at ``info().sample_rate``."""

    def _unload(self) -> None:
        """Drop references to the loaded model so its VRAM can be freed. Override
        in engines that hold a model (e.g. ``self._model = None``). Default: no-op."""

    def load(self) -> None:
        self._load_error = None
        try:
            self._load()
            self._ready = True
        except Exception as exc:  # noqa: BLE001 - surfaced via /health, not fatal
            self._load_error = f"{type(exc).__name__}: {exc}"
            # A load that fails partway may have left half-allocated GPU state on
            # the card. Drop it best-effort so a retry (ensure_loaded) doesn't
            # stack a second partial allocation on top and hasten VRAM exhaustion.
            try:
                self._unload()
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass

    def unload(self) -> None:
        """Free the model from memory/VRAM. The engine reloads lazily on its next
        synthesis. Safe to call when nothing is loaded."""
        try:
            self._unload()
        finally:
            self._ready = False
            self._load_error = None
            # Collect Python garbage *before* releasing CUDA blocks: PyTorch models
            # hold reference cycles (module graphs, hooks, autograd buffers) that
            # pin CUDA tensors, so empty_cache() reclaims almost nothing until those
            # cycles are broken. gc first, then hand the freed blocks back to CUDA.
            import gc

            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
            except Exception:  # noqa: BLE001 - torch absent (CPU engines) is fine
                pass

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def load_error(self) -> str | None:
        return self._load_error


def float_to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """Encode mono float32 [-1,1] audio as 16-bit PCM WAV bytes."""
    samples = np.clip(np.asarray(audio, dtype=np.float32).flatten(), -1.0, 1.0)
    pcm = (samples * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def decode_b64_to_tempfile(b64: str, suffix: str = ".wav") -> str:
    """Write a base64 audio payload to a temp file and return its path.

    Caller is responsible for deleting the file (see ``cleanup_files``).
    """
    data = base64.b64decode(b64)
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def cleanup_files(*paths: str | None) -> None:
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def create_engine_app(engine: TTSEngine, *, lazy: bool | None = None) -> FastAPI:
    """Wrap a ``TTSEngine`` into a ready-to-serve FastAPI application.

    ``lazy`` (default from the ``LAZY_LOAD`` env var) defers model loading to the
    first ``/synthesize`` call instead of startup. This is essential when several
    GPU engines share one card: a lazy engine that's *registered* but never used
    holds **zero** VRAM, so all engines can show as available while only the ones
    you actually synthesize with load their weights.
    """
    if lazy is None:
        lazy = os.environ.get("LAZY_LOAD", "0").strip().lower() in ("1", "true", "yes")
    # Per-engine override: always free VRAM after each synth (request flag can also
    # trigger it). Useful on a small GPU shared by several engines.
    unload_default = os.environ.get("UNLOAD_AFTER_SYNTH", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    # One lock guards every mutating operation (load / synthesize / unload) so they
    # are mutually exclusive on a given engine. Crucially this stops a concurrent
    # /unload (or another request's unload_after) from freeing the model — and
    # calling torch.cuda.empty_cache() — while a synthesis is mid-flight, which
    # would otherwise crash with a NoneType/CUDA error or corrupt the output. It's
    # reentrant so ensure_loaded() can be called from inside synthesize().
    op_lock = threading.RLock()

    def ensure_loaded() -> None:
        if engine.ready:
            return
        with op_lock:
            if not engine.ready:
                engine.load()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Eager engines warm up at startup; lazy ones wait for the first request.
        if not lazy:
            engine.load()
        yield

    meta = engine.info()
    app = FastAPI(title=f"TTS Engine · {meta.name}", lifespan=lifespan)

    @app.get("/health")
    def health() -> JSONResponse:
        # A lazy engine is "ready" (available) as soon as the service is up; it
        # reports ``loaded`` separately so the UI can hint "loads on first run".
        ready = engine.ready or lazy
        return JSONResponse(
            {
                "id": meta.id,
                "ready": ready,
                "loaded": engine.ready,
                "lazy": lazy,
                "error": engine.load_error,
            },
            status_code=200 if ready else 503,
        )

    @app.get("/info", response_model=EngineInfo)
    def info() -> EngineInfo:
        return engine.info()

    @app.post("/synthesize")
    def synthesize(req: SynthRequest) -> Response:
        # Hold op_lock across the whole request so no /unload can free the model
        # underneath an in-flight synthesis (see the lock comment above).
        with op_lock:
            if lazy and not engine.ready:
                ensure_loaded()  # blocking first-load (model download / VRAM alloc)
            if not engine.ready:
                raise HTTPException(503, engine.load_error or "engine not ready")
            started = time.perf_counter()
            try:
                audio = engine.synthesize(req)
            except ValueError as exc:
                # Bad caller input (e.g. a required reference clip is missing) —
                # a 4xx, not a server fault.
                raise HTTPException(400, f"invalid request: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(500, f"synthesis failed: {exc}") from exc
            gen_s = time.perf_counter() - started
            wav = float_to_wav_bytes(audio, meta.sample_rate)
            audio_s = float(len(audio)) / meta.sample_rate if len(audio) else 0.0
            rtf = gen_s / audio_s if audio_s else 0.0
            unloaded = req.unload_after or unload_default
            if unloaded:
                engine.unload()  # free VRAM for the next engine
        return Response(
            content=wav,
            media_type="audio/wav",
            headers={
                "X-Engine-Id": meta.id,
                "X-Gen-Seconds": f"{gen_s:.3f}",
                "X-Audio-Seconds": f"{audio_s:.3f}",
                "X-RTF": f"{rtf:.3f}",
                "X-Sample-Rate": str(meta.sample_rate),
                "X-Unloaded": "1" if unloaded else "0",
            },
        )

    @app.post("/unload")
    def unload() -> JSONResponse:
        """Free the model from VRAM now (manual). Reloads lazily on next synth.
        Waits for any in-flight synthesis to finish (op_lock) before freeing."""
        with op_lock:
            engine.unload()
        return JSONResponse({"id": meta.id, "loaded": engine.ready})

    return app
