"""StyleTTS2 adapter — long-form prosody from a reference clip. Style (and a degree of emotion) comes from a reference clip; we map
our ``emotion`` 0..1 onto ``embedding_scale`` (1.0..2.0) so the knob increases
expressiveness. ``diffusion_steps`` is exposed via ``extra`` (higher = better,
slower).

Uses the community ``styletts2`` pip package, which bundles the LibriTTS
checkpoint and downloads it on first use."""

from __future__ import annotations

import numpy as np

from shared.engine_base import TTSEngine, cleanup_files, decode_b64_to_tempfile
from shared.schemas import Capabilities, EngineInfo, SynthRequest


class StyleTTS2Engine(TTSEngine):
    SR = 24_000

    def __init__(self) -> None:
        super().__init__()
        self._model = None

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="styletts2",
            name="StyleTTS 2",
            description=(
                "Long-form prosody; voice and style from a reference clip. Pretrained "
                "models require telling listeners the speech is synthetic."
            ),
            license="MIT code; pretrained-model terms: disclose synthetic speech",
            homepage="https://github.com/yl4579/StyleTTS2",
            sample_rate=self.SR,
            capabilities=Capabilities(
                cloning=True,
                emotion_scalar=True,
                emotion_reference=True,
                requires_gpu=True,
                languages=["en"],
            ),
        )

    def _load(self) -> None:
        from styletts2 import tts

        self._model = tts.StyleTTS2()

    def _unload(self) -> None:
        self._model = None

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        ref = (
            decode_b64_to_tempfile(req.reference_audio_b64)
            if req.reference_audio_b64
            else None
        )
        try:
            embedding_scale = 1.0 + (req.emotion if req.emotion is not None else 0.0)
            audio = self._model.inference(
                req.text,
                target_voice_path=ref,
                diffusion_steps=int(req.extra.get("diffusion_steps", 8)),
                embedding_scale=embedding_scale,
                output_sample_rate=self.SR,
            )
            return np.asarray(audio, dtype=np.float32).flatten()
        finally:
            cleanup_files(ref)
