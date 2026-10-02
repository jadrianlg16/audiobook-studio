"""IndexTTS2 adapter tests — no GPU, no weights, no torch, no `indextts` package.

**What these tests are, and what they are not.** The adapter claims
IndexTTS2 can read a written director's note by passing it as ``emo_text``. That
claim is a HYPOTHESIS and only a bake-off on real weights can settle it. These
tests settle a smaller and still useful question: *exactly what would be sent*.
When someone finally runs it on a card and it sounds wrong, this file is the
difference between "the mapping is wrong" and "the model can't do it".

So: a fake stands in for ``IndexTTS2.infer`` and records its kwargs. The heavy
imports (``torch``, ``indextts``) live inside ``_load``, which is never called;
``librosa`` and ``soundfile`` are imported inside ``synthesize``, so they are
injected into ``sys.modules`` for the duration of a test. That is what lets this
run on a laptop with an integrated GPU.

Run from the project root:  pytest engines/indextts2/tests
"""

from __future__ import annotations

import base64
import sys
import types
import wave

import numpy as np
import pytest

from engines.indextts2.tts import IndexTTS2Engine
from shared.schemas import SynthRequest

_SR = 24_000


def _wav_b64(seconds: float = 0.1) -> str:
    """A real (tiny) WAV, because the adapter writes the reference to a temp file."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(b"\x00\x00" * int(_SR * seconds))
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _FakeTTS:
    """Stands in for ``indextts.infer_v2.IndexTTS2``; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def infer(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def engine(monkeypatch):
    """A loaded-looking engine whose model is a recorder, with stub audio libs."""
    soundfile = types.ModuleType("soundfile")
    soundfile.read = lambda path, dtype="float32": (  # noqa: ARG005
        np.zeros(_SR // 10, dtype=np.float32),
        _SR,
    )
    librosa = types.ModuleType("librosa")

    def _no_resample(*a, **kw):  # the stub already returns the engine's own rate
        raise AssertionError("resample should not run when the rates already match")

    librosa.resample = _no_resample
    monkeypatch.setitem(sys.modules, "soundfile", soundfile)
    monkeypatch.setitem(sys.modules, "librosa", librosa)

    eng = IndexTTS2Engine()
    eng._tts = _FakeTTS()
    return eng


def _synth(engine: IndexTTS2Engine, **fields) -> dict:
    """Run one synthesis and return the kwargs the model was called with."""
    fields.setdefault("text", "It was over, and she had known for weeks.")
    fields.setdefault("reference_audio_b64", _wav_b64())
    audio = engine.synthesize(SynthRequest(**fields))
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
    return engine._tts.calls[-1]


# ---- what the engine advertises ---------------------------------------------

def test_info_advertises_written_direction() -> None:
    """The director only offers a note to engines that declare they take one, so
    this flag is what puts IndexTTS2 in the directable set at all."""
    info = IndexTTS2Engine().info()
    assert info.capabilities.instruction is True
    assert info.id == "indextts2"


def test_info_advertises_no_audio_tags() -> None:
    """IndexTTS2 has no inline vocal events. An empty list must read as 'never
    inject any' — a (sigh) sent here is spoken aloud as the word."""
    assert IndexTTS2Engine().info().audio_tags == []


def test_the_emotion_labels_are_still_offered_as_the_fallback() -> None:
    info = IndexTTS2Engine().info()
    assert "neutral" in info.emotion_labels and "sad" in info.emotion_labels


# ---- exactly what would be sent to the model --------------------------------

def test_an_instruction_is_sent_as_emo_text_with_use_emo_text(engine) -> None:
    note = "Speak slowly and low, holding the tension on the last word of each sentence."
    call = _synth(engine, instruction=note)
    assert call["use_emo_text"] is True
    assert call["emo_text"] == note


def test_the_instruction_wins_over_the_emotion_label(engine) -> None:
    """There is only one ``emo_text``, and a prose note is strictly more specific
    than a one-word label. Sending the label instead would silently discard the
    director's whole contribution."""
    call = _synth(engine, instruction="Restrained, almost a whisper.", emotion_label="happy")
    assert call["emo_text"] == "Restrained, almost a whisper."


def test_the_label_remains_the_fallback_when_there_is_no_instruction(engine) -> None:
    """Kokoro-era books and the manual UI controls still send labels only."""
    call = _synth(engine, emotion_label="sad")
    assert call["use_emo_text"] is True and call["emo_text"] == "sad"


def test_neither_one_leaves_emotion_text_off_entirely(engine) -> None:
    """Not ``emo_text=""`` — the key must be absent, or an undirected line would
    be conditioned on an empty description."""
    call = _synth(engine)
    assert "use_emo_text" not in call and "emo_text" not in call


def test_the_text_and_speaker_prompt_are_passed_through(engine) -> None:
    call = _synth(engine, text="She put the letter down.", instruction="Quietly.")
    assert call["text"] == "She put the letter down."
    assert call["spk_audio_prompt"]  # a temp file path holding the reference clip


def test_the_emotion_scalar_becomes_emo_alpha(engine) -> None:
    assert _synth(engine, emotion=0.75, instruction="Tense.")["emo_alpha"] == 0.75


def test_an_emotion_reference_clip_is_passed_alongside_the_note(engine) -> None:
    call = _synth(engine, instruction="Tender.", emotion_reference_b64=_wav_b64())
    assert call["emo_audio_prompt"]
    assert call["emo_text"] == "Tender."


def test_a_missing_speaker_clip_is_refused_loudly(engine) -> None:
    """IndexTTS2 cannot synthesize without a voice to clone; failing here beats
    failing forty scenes into a book."""
    with pytest.raises(ValueError, match="reference"):
        engine.synthesize(SynthRequest(text="Hello.", instruction="Warmly."))
