"""Mock engine: a pure-numpy sine-tone generator. No models, no downloads, no
GPU. Its only job is to let you exercise the entire stack (gateway, bake-off,
UI) offline and instantly. The ``emotion`` knob maps to vibrato depth so the
control is audibly wired end to end."""

from __future__ import annotations

import numpy as np

from shared.engine_base import TTSEngine
from shared.schemas import Capabilities, EngineInfo, SynthRequest, VoiceInfo


class MockEngine(TTSEngine):
    SR = 24_000

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="mock",
            name="Mock Tone",
            description="Offline sine-tone generator for testing the stack without any model.",
            license="n/a (no model weights)",
            sample_rate=self.SR,
            capabilities=Capabilities(
                multi_speaker=True, emotion_scalar=True, requires_gpu=False
            ),
            voices=[
                VoiceInfo(id="low", label="Low (220 Hz)"),
                VoiceInfo(id="mid", label="Mid (440 Hz)"),
                VoiceInfo(id="high", label="High (660 Hz)"),
            ],
        )

    def _load(self) -> None:
        # Nothing to load.
        return None

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        base = {"low": 220.0, "mid": 440.0, "high": 660.0}.get(req.voice or "mid", 440.0)
        duration = max(0.5, min(20.0, len(req.text) * 0.08 / req.speed))
        t = np.linspace(0.0, duration, int(self.SR * duration), endpoint=False)

        # emotion -> vibrato depth, so the slider produces an audible change.
        depth = (req.emotion or 0.0) * 6.0
        vibrato = depth * np.sin(2 * np.pi * 5.0 * t)
        tone = 0.2 * np.sin(2 * np.pi * base * t + vibrato)

        # 10 ms fades to avoid clicks at the boundaries.
        fade = int(self.SR * 0.01)
        env = np.ones_like(tone)
        env[:fade] = np.linspace(0.0, 1.0, fade)
        env[-fade:] = np.linspace(1.0, 0.0, fade)
        return (tone * env).astype(np.float32)
