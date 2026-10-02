"""IndexTTS2 adapter (Bilibili). Upstream describes it as disentangling speaker
timbre from emotion; it takes a separate emotion reference or a natural-language
emotion description, and supports duration control, which is why it is wired to
the director here.

It's distributed as a repo + checkpoints rather than a clean wheel, so:
  - the package is pip-installed from GitHub in the Dockerfile, and
  - weights are expected at INDEXTTS_MODEL_DIR (bind-mounted, see compose).

Output is resampled to ``SR`` so pitch is consistent with the other engines.

⚠️ Verify IndexTTS2's license before any commercial use."""

from __future__ import annotations

import os
import tempfile

import numpy as np

from shared.engine_base import TTSEngine, cleanup_files, decode_b64_to_tempfile
from shared.schemas import Capabilities, EngineInfo, SynthRequest

# Emotion descriptions usable with use_emo_text=True.
_EMOTIONS = ["neutral", "happy", "sad", "angry", "afraid", "surprised", "calm", "excited"]


class IndexTTS2Engine(TTSEngine):
    SR = 24_000

    def __init__(self) -> None:
        super().__init__()
        self._tts = None

    def info(self) -> EngineInfo:
        return EngineInfo(
            id="indextts2",
            name="IndexTTS 2",
            description=(
                "Directable emotion: timbre and emotion are disentangled. Takes an "
                "emotion label/clip + intensity + duration control. Needs a speaker clip."
            ),
            license="bilibili Model Use License (see upstream LICENSE)",
            homepage="https://github.com/index-tts/index-tts",
            sample_rate=self.SR,
            capabilities=Capabilities(
                cloning=True,
                emotion_tags=True,
                emotion_scalar=True,
                emotion_reference=True,
                # IndexTTS2 takes a natural-language emotion description through
                # the same `emo_text` channel as a one-word label, so it can in
                # principle read a director's prose note. See the HYPOTHESIS in
                # `synthesize` — this flag is what makes the director *offer* it
                # one, which is how the hypothesis gets tested at all.
                instruction=True,
                requires_gpu=True,
                languages=["en", "zh"],
            ),
            emotion_labels=_EMOTIONS,
            # No inline tag vocabulary: IndexTTS2 has no (sigh)-style events, so
            # the director must never insert one — it would be read aloud.
            audio_tags=[],
        )

    def _load(self) -> None:
        from indextts.infer_v2 import IndexTTS2

        model_dir = os.environ.get("INDEXTTS_MODEL_DIR", "/models/IndexTTS-2")
        self._tts = IndexTTS2(
            cfg_path=os.path.join(model_dir, "config.yaml"),
            model_dir=model_dir,
            use_fp16=True,
        )

    def _unload(self) -> None:
        self._tts = None

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        import librosa
        import soundfile as sf

        # IndexTTS2 needs a speaker prompt; reuse the emotion ref as speaker if
        # only one clip was provided.
        speaker = (
            decode_b64_to_tempfile(req.reference_audio_b64)
            if req.reference_audio_b64
            else None
        )
        emo_ref = (
            decode_b64_to_tempfile(req.emotion_reference_b64)
            if req.emotion_reference_b64
            else None
        )
        out_fd, out_path = tempfile.mkstemp(suffix=".wav")
        os.close(out_fd)
        try:
            if speaker is None:
                raise ValueError("IndexTTS2 requires a reference (speaker) clip")
            kwargs: dict = {
                "spk_audio_prompt": speaker,
                "text": req.text,
                "output_path": out_path,
                "verbose": False,
            }
            # ⚠️ HYPOTHESIS — UNTESTED ON REAL WEIGHTS.
            #
            # IndexTTS2's `emo_text` is documented as a natural-language emotion
            # description, so a written performance note ("Speak slowly and low,
            # holding tension on the last word") is fed down the same channel as
            # the one-word label. That is the whole claim, and nothing here
            # verifies it: the published examples are short emotion *words*, and
            # the text is embedded by a small emotion encoder that may well have
            # been trained on that shape. A two-sentence director's note could be
            # truncated, averaged into mush, or ignored.
            #
            # Do NOT treat a green test suite as evidence — these tests only pin
            # what we would *send*. One bake-off on a GPU against the same lines
            # with (a) a label and (b) a note decides it. If notes lose, drop
            # `Capabilities.instruction` back to False and IndexTTS2 stays a
            # label engine; the director already falls back to labels for it.
            #
            # Instruction wins over the label when both are present — a note is
            # strictly more specific, and sending both is not an option since
            # there is only one `emo_text`.
            emo_text = req.instruction or req.emotion_label
            if emo_text:
                kwargs.update(use_emo_text=True, emo_text=emo_text)
            if emo_ref:
                kwargs["emo_audio_prompt"] = emo_ref
            if req.emotion is not None:
                kwargs["emo_alpha"] = req.emotion
            self._tts.infer(**kwargs)

            audio, sr = sf.read(out_path, dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            if sr != self.SR:
                audio = librosa.resample(audio, orig_sr=sr, target_sr=self.SR)
            return np.asarray(audio, dtype=np.float32)
        finally:
            cleanup_files(speaker, emo_ref, out_path)
