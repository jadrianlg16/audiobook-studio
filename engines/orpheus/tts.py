"""Orpheus 3B adapter (Canopy Labs, Apache-2.0). A Llama-3 speech LLM with
built-in expressive voices and inline emotion tags like ``<laugh>`` / ``<sigh>``.

Note the known trade-off: emotion tags are reliable with the *built-in* voices,
not with arbitrary zero-shot clones — so this adapter exposes named voices +
tags rather than cloning. We accept an ``emotion_label`` and inject it as an
inline tag; the AI director can also embed tags directly in ``text``.

Runs on vLLM and needs a CUDA GPU."""

from __future__ import annotations

import numpy as np

from shared.engine_base import TTSEngine
from shared.schemas import Capabilities, EngineInfo, SynthRequest, VoiceInfo

_VOICES = ["tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"]
_TAGS = ["<laugh>", "<chuckle>", "<sigh>", "<cough>", "<sniffle>", "<groan>", "<yawn>", "<gasp>"]


class OrpheusEngine(TTSEngine):
    SR = 24_000

    def __init__(self) -> None:
        super().__init__()
        self._model = None

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="orpheus",
            name="Orpheus 3B",
            description=(
                "Apache-2.0. Expressive Llama-3 speech LLM with built-in voices "
                "and inline emotion tags (<laugh>, <sigh>, ...). vLLM/GPU."
            ),
            license="Apache-2.0 (model card); fine-tuned from Llama 3.2",
            homepage="https://github.com/canopyai/Orpheus-TTS",
            sample_rate=self.SR,
            capabilities=Capabilities(
                multi_speaker=True, emotion_tags=True, requires_gpu=True, languages=["en"]
            ),
            voices=[VoiceInfo(id=v, label=v.capitalize()) for v in _VOICES],
            emotion_labels=_TAGS,
        )

    def _load(self) -> None:
        from orpheus_tts import OrpheusModel

        self._model = OrpheusModel(model_name="canopylabs/orpheus-3b-0.1-ft")

    def _unload(self) -> None:
        # vLLM doesn't fully release VRAM on del, but this drops our reference and
        # lets empty_cache reclaim what it can.
        self._model = None

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        voice = req.voice or "tara"
        prompt = req.text
        # Only inject a tag Orpheus actually knows — an unknown label would be
        # spoken aloud verbatim. The director may also inline tags within the text.
        if req.emotion_label and req.emotion_label.strip().lower() in _TAGS:
            prompt = f"{req.emotion_label.strip().lower()} {prompt}"
        # Accumulate raw bytes: individual streamed chunks aren't guaranteed to be
        # an even number of bytes, so converting each with frombuffer(int16) can
        # raise "buffer size must be a multiple of element size". Buffer, then
        # convert only whole 2-byte samples (a trailing odd byte is dropped).
        buf = bytearray()
        for piece in self._model.generate_speech(prompt=prompt, voice=voice):
            buf.extend(piece)
        usable = len(buf) - (len(buf) % 2)
        if usable == 0:
            return np.zeros(1, dtype=np.float32)
        samples = np.frombuffer(bytes(buf[:usable]), dtype=np.int16)
        return samples.astype(np.float32) / 32768.0
