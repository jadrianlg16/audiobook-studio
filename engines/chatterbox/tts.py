"""Chatterbox adapter (Resemble AI, MIT). Zero-shot voice cloning plus a single
``exaggeration`` knob mapped to our ``emotion`` field. ``cfg_weight`` is exposed
through ``extra`` (lower it when you push exaggeration high, per upstream advice).

Requires a CUDA GPU in practice. If torch/cuda or weights are unavailable,
``_load`` raises and the gateway simply lists this engine as unavailable."""

from __future__ import annotations

import numpy as np

from shared.engine_base import TTSEngine, cleanup_files, decode_b64_to_tempfile
from shared.schemas import Capabilities, EngineInfo, SynthRequest


class ChatterboxEngine(TTSEngine):
    SR = 24_000  # Chatterbox output sample rate (model.sr)

    def __init__(self) -> None:
        super().__init__()
        self._model = None

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="chatterbox",
            name="Chatterbox",
            description=(
                "MIT. Zero-shot cloning + one expressiveness (exaggeration) knob; "
                "emotion is intensity, not a category."
            ),
            license="MIT",
            homepage="https://github.com/resemble-ai/chatterbox",
            sample_rate=self.SR,
            capabilities=Capabilities(
                cloning=True, emotion_scalar=True, requires_gpu=True, languages=["en"]
            ),
        )

    def _load(self) -> None:
        import torch
        from chatterbox.tts import ChatterboxTTS

        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = ChatterboxTTS.from_pretrained(device=device)

    def _unload(self) -> None:
        self._model = None

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        import torch

        ref = (
            decode_b64_to_tempfile(req.reference_audio_b64)
            if req.reference_audio_b64
            else None
        )
        try:
            if req.seed is not None:
                torch.manual_seed(req.seed)
            kwargs: dict = {
                "exaggeration": req.emotion if req.emotion is not None else 0.5,
                "cfg_weight": float(req.extra.get("cfg_weight", 0.5)),
            }
            if ref:
                kwargs["audio_prompt_path"] = ref
            wav = self._model.generate(req.text, **kwargs)
            return wav.squeeze().detach().cpu().numpy().astype(np.float32)
        finally:
            cleanup_files(ref)
