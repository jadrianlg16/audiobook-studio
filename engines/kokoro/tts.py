"""Kokoro-82M adapter — fast, Apache-2.0, runs on CPU. The "clean neutral
narrator" baseline. No voice cloning and no emotion control by design; it ships
fixed high-quality voices.

Model weights (~330 MB) are downloaded from HuggingFace on first ``_load`` and
cached under HF_HOME (bind-mounted in compose)."""

from __future__ import annotations

import numpy as np

from shared.engine_base import TTSEngine
from shared.schemas import Capabilities, EngineInfo, SynthRequest, VoiceInfo

# A curated subset of Kokoro's 50+ voices (US/UK, M/F).
_VOICES: list[tuple[str, str]] = [
    ("af_heart", "Heart · US Female"),
    ("af_bella", "Bella · US Female"),
    ("af_nicole", "Nicole · US Female"),
    ("am_michael", "Michael · US Male"),
    ("am_fenrir", "Fenrir · US Male"),
    ("bf_emma", "Emma · UK Female"),
    ("bm_george", "George · UK Male"),
]


class KokoroEngine(TTSEngine):
    SR = 24_000

    def __init__(self) -> None:
        super().__init__()
        self._pipeline = None

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="kokoro",
            name="Kokoro-82M",
            description=(
                "Fast, Apache-2.0, CPU-capable. Clean, consistent narration. "
                "No cloning or emotion control — the steady baseline."
            ),
            license="Apache-2.0",
            homepage="https://huggingface.co/hexgrad/Kokoro-82M",
            sample_rate=self.SR,
            capabilities=Capabilities(
                multi_speaker=True, requires_gpu=False, languages=["en"]
            ),
            voices=[VoiceInfo(id=v, label=lbl) for v, lbl in _VOICES],
        )

    def _load(self) -> None:
        from kokoro import KPipeline

        # 'a' = American English. Voices download lazily on first use.
        self._pipeline = KPipeline(lang_code="a")
        # Warm the default voice so the first real request isn't slow.
        list(self._pipeline("Warm up.", voice="af_heart"))

    def _unload(self) -> None:
        self._pipeline = None

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        voice = req.voice or "af_heart"
        chunks: list[np.ndarray] = []
        for _, _, audio in self._pipeline(req.text, voice=voice, speed=req.speed):
            chunks.append(np.asarray(audio, dtype=np.float32))
        if not chunks:
            return np.zeros(1, dtype=np.float32)
        return np.concatenate(chunks)
