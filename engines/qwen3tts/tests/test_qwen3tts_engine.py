"""Qwen3-TTS adapter tests — no GPU, no weights, no network, no real torch.

A fake ``qwen_tts`` module and a fake ``torch`` module are injected into
``sys.modules`` for every test, so the adapter's *real* ``_load`` runs (device,
dtype, attention choice, checkpoint-type guard) through the *real* engine app
(lazy load, op_lock, /unload) against a recorder instead of a model. The
checkpoint's config.json is a trimmed copy of the real one (Qwen/Qwen3-TTS-12Hz-
1.7B-CustomVoice @ 0c0e305, 2026-09-26), written to a temp dir.

What these tests pin is *exactly what the model would be asked to do* — which
speaker, which language, which note, which seed. Whether a note audibly changes
the delivery is a GPU question; see engines/qwen3tts/README.md for that run.

Run from the project root:  pytest engines/qwen3tts/tests
"""

from __future__ import annotations

import io
import json
import sys
import types
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

import engines.qwen3tts.tts as qtts
from engines.qwen3tts.tts import Qwen3TTSEngine, derive_seed
from shared.engine_base import create_engine_app
from shared.schemas import SynthRequest

_SR = 24_000

# The parts of the real config.json the adapter reads (speaker ids, language ids,
# dialect flags, model type/size), copied from the checkpoint.
_REAL_CONFIG = {
    "tts_model_type": "custom_voice",
    "tts_model_size": "1b7",
    "tokenizer_type": "qwen3_tts_tokenizer_12hz",
    "talker_config": {
        "spk_id": {
            "serena": 3066, "vivian": 3065, "uncle_fu": 3010, "ryan": 3061, "aiden": 2861,
            "ono_anna": 2873, "sohee": 2864, "eric": 2875, "dylan": 2878,
        },
        "spk_is_dialect": {
            "serena": False, "vivian": False, "uncle_fu": False, "ryan": False, "aiden": False,
            "ono_anna": False, "sohee": False, "eric": "sichuan_dialect", "dylan": "beijing_dialect",
        },
        "codec_language_id": {
            "chinese": 2055, "english": 2050, "german": 2053, "italian": 2070, "portuguese": 2071,
            "spanish": 2054, "japanese": 2058, "korean": 2064, "french": 2061, "russian": 2069,
            "beijing_dialect": 2074, "sichuan_dialect": 2062,
        },
    },
}
_SPEAKERS = set(_REAL_CONFIG["talker_config"]["spk_id"])
_LANG_CODES = {"en", "zh", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"}


# ---- fakes ------------------------------------------------------------------

class _FakeInner:
    def __init__(self, model_type: str, size: str) -> None:
        self.tts_model_type = model_type
        self.tts_model_size = size


class FakeQwen:
    """Stands in for ``qwen_tts.Qwen3TTSModel``; records every generate call."""

    def __init__(self, *, model_type="custom_voice", size="1b7", speakers=None, sr=_SR) -> None:
        self.model = _FakeInner(model_type, size)
        self._speakers = sorted(speakers if speakers is not None else _SPEAKERS)
        self._sr = sr
        self.calls: list[dict] = []

    def get_supported_speakers(self):
        return list(self._speakers)

    def get_supported_languages(self):
        langs = [k for k in _REAL_CONFIG["talker_config"]["codec_language_id"] if "dialect" not in k]
        return sorted(["auto", *langs])

    def generate_custom_voice(self, **kwargs):
        self.calls.append(kwargs)
        return [np.full(_SR // 10, 0.1, dtype=np.float32)], self._sr


class _Loader:
    """The fake ``qwen_tts`` module: records from_pretrained and hands out a model."""

    def __init__(self) -> None:
        self.loads: list[tuple[str, dict]] = []
        self.make = lambda: FakeQwen()
        self.last: FakeQwen | None = None

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("qwen_tts")
        loader = self

        class Qwen3TTSModel:
            @classmethod
            def from_pretrained(cls, repo, **kwargs):
                loader.loads.append((repo, kwargs))
                loader.last = loader.make()
                return loader.last

        mod.Qwen3TTSModel = Qwen3TTSModel
        return mod


def _fake_torch() -> types.ModuleType:
    torch = types.ModuleType("torch")
    torch.bfloat16, torch.float16, torch.float32 = "bf16", "fp16", "fp32"
    cuda = types.SimpleNamespace(
        is_available=lambda: False,
        empty_cache=lambda: None,
        ipc_collect=lambda: None,
    )
    torch.cuda = cuda
    torch.manual_seed = lambda seed: None
    return torch


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """Every test: real-shaped config in a temp dir, fake torch + qwen_tts, and a
    seed recorder in place of the real torch seeding."""
    for name in ("QWEN_TTS_MODEL", "QWEN_TTS_DEFAULT_VOICE", "QWEN_TTS_DEVICE",
                 "QWEN_TTS_DTYPE", "QWEN_TTS_ATTN", "UNLOAD_AFTER_SYNTH"):
        monkeypatch.delenv(name, raising=False)
    ckpt = tmp_path / "Qwen3-TTS-12Hz-1.7B-CustomVoice"
    ckpt.mkdir()
    (ckpt / "config.json").write_text(json.dumps(_REAL_CONFIG), encoding="utf-8")
    monkeypatch.setenv("QWEN_TTS_MODEL", str(ckpt))

    loader = _Loader()
    monkeypatch.setitem(sys.modules, "qwen_tts", loader.module())
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())

    seeds: list[int] = []
    monkeypatch.setattr(qtts, "_seed_torch", seeds.append)
    return types.SimpleNamespace(ckpt=ckpt, loader=loader, seeds=seeds)


@pytest.fixture
def engine() -> Qwen3TTSEngine:
    """An engine whose model is already a recorder (skips _load)."""
    eng = Qwen3TTSEngine()
    eng._model = FakeQwen()
    eng._ready = True
    return eng


def _call(engine: Qwen3TTSEngine, **fields) -> dict:
    fields.setdefault("text", "The door creaked open.")
    audio = engine.synthesize(SynthRequest(**fields))
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32
    return engine._model.calls[-1]


def _client() -> tuple[TestClient, Qwen3TTSEngine]:
    eng = Qwen3TTSEngine()
    return TestClient(create_engine_app(eng, lazy=True)), eng


# ---- /info tells the truth ----------------------------------------------------

def test_info_declares_instruction_and_named_voices_and_nothing_else() -> None:
    info = Qwen3TTSEngine().info()
    caps = info.capabilities
    assert info.id == "qwen3tts"
    assert caps.instruction is True  # the note goes to `instruct`
    assert caps.multi_speaker is True
    assert caps.requires_gpu is True
    # Not wired / not supported by CustomVoice — must not be advertised.
    assert caps.cloning is False
    assert caps.emotion_scalar is False
    assert caps.emotion_tags is False
    assert caps.emotion_reference is False
    assert info.emotion_labels == []
    assert info.sample_rate == _SR
    assert "Apache-2.0" in info.license


def test_no_audio_tags_are_advertised() -> None:
    """The Breeze lesson: only advertise inline events the weights attest. Nothing
    in the Qwen3-TTS weights, card or package attests any, and an empty list is
    what stops the director from inserting "(sigh)" to be read aloud."""
    assert Qwen3TTSEngine().info().audio_tags == []


def test_voices_are_the_checkpoints_speakers_with_an_english_default_first() -> None:
    voices = Qwen3TTSEngine().info().voices
    assert {v.id for v in voices} == _SPEAKERS
    # The lab shows voices[0] as the pre-selected voice: an English native.
    assert voices[0].id == "aiden"
    assert voices[0].language == "en"
    assert "English native" in voices[0].label and "default" in voices[0].label
    by_id = {v.id: v for v in voices}
    assert by_id["ryan"].language == "en"
    assert by_id["ono_anna"].language == "ja" and by_id["sohee"].language == "ko"
    assert "Sichuan" in by_id["eric"].label and "Beijing" in by_id["dylan"].label


def test_languages_are_the_checkpoints_list_as_iso_codes() -> None:
    langs = Qwen3TTSEngine().info().capabilities.languages
    assert set(langs) == _LANG_CODES  # dialects and "auto" are not languages
    assert langs[0] == "en"


def test_fallback_facts_before_download_match_the_real_checkpoint(monkeypatch) -> None:
    """With no config.json on disk yet, /info reports the default checkpoint's
    published facts — pinned here against the real config so they can't drift."""
    monkeypatch.setattr(qtts, "_cached_config_path", lambda model: None)
    info = Qwen3TTSEngine().info()
    assert {v.id for v in info.voices} == _SPEAKERS
    assert set(info.capabilities.languages) == _LANG_CODES
    assert info.capabilities.instruction is True


def test_the_0_6b_checkpoint_does_not_claim_instruction(env) -> None:
    """Upstream drops `instruct` on the 0.6B CustomVoice, so advertising it would
    make the director write notes that change nothing."""
    cfg = dict(_REAL_CONFIG, tts_model_size="0b6")
    (env.ckpt / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    eng = Qwen3TTSEngine()
    assert eng.info().capabilities.instruction is False
    eng._model = FakeQwen(size="0b6")
    eng._ready = True
    assert _call(eng, instruction="Whispering.")["instruct"] is None


def test_a_loaded_model_answers_for_itself(engine) -> None:
    engine._model = FakeQwen(speakers=sorted(_SPEAKERS | {"newvoice"}))
    assert "newvoice" in {v.id for v in engine.info().voices}


def test_the_default_voice_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_TTS_DEFAULT_VOICE", "Ryan")
    assert Qwen3TTSEngine().info().voices[0].id == "ryan"
    monkeypatch.setenv("QWEN_TTS_DEFAULT_VOICE", "nobody")
    assert Qwen3TTSEngine().info().voices[0].id == "aiden"


# ---- exactly what reaches the model -----------------------------------------

def test_the_instruction_is_passed_through_as_instruct(engine) -> None:
    call = _call(engine, instruction="whispering, trembling with fear")
    assert call["instruct"] == "whispering, trembling with fear"


@pytest.mark.parametrize("instruction", [None, "", "   "])
def test_no_instruction_sends_instruct_none(engine, instruction) -> None:
    """None, not "" — upstream treats both as "no note", but None is the
    unambiguous one and the one this adapter promises."""
    assert _call(engine, instruction=instruction)["instruct"] is None


def test_text_voice_and_language_reach_the_model(engine) -> None:
    call = _call(engine, text="She put the letter down.", voice="Ryan", language="en")
    assert call["text"] == "She put the letter down."
    assert call["speaker"] == "ryan"
    assert call["language"] == "English"
    assert call["non_streaming_mode"] is True


def test_no_voice_means_the_default_voice(engine) -> None:
    assert _call(engine)["speaker"] == "aiden"


@pytest.mark.parametrize(("sent", "expected"), [("auto", "Auto"), ("English", "English"), ("ja", "Japanese")])
def test_language_accepts_iso_codes_model_names_and_auto(engine, sent, expected) -> None:
    assert _call(engine, language=sent)["language"] == expected


def test_emotion_label_and_speed_are_not_forwarded(engine) -> None:
    """No rate control and no label vocabulary: ignored, per the contract, and
    not smuggled into instruct either (pace goes in the note)."""
    call = _call(engine, speed=1.5, emotion_label="sad", emotion=0.9)
    assert call["instruct"] is None
    assert not {"speed", "emotion", "emotion_label"} & set(call)


def test_only_known_generation_knobs_pass_through_extra(engine) -> None:
    call = _call(engine, extra={"temperature": 0.5, "do_sample": False, "cfg_scale": 4})
    assert call["temperature"] == 0.5 and call["do_sample"] is False
    assert "cfg_scale" not in call


def test_an_unexpected_sample_rate_is_an_error_not_wrong_pitch(engine) -> None:
    engine._model = FakeQwen(sr=16_000)
    with pytest.raises(RuntimeError, match="16000"):
        engine.synthesize(SynthRequest(text="Hello."))


# ---- seeding ------------------------------------------------------------------

def test_the_seed_ignores_the_instruction(engine, env) -> None:
    """The A/B guarantee: same line, same voice -> same seed, note or no note."""
    _call(engine, text="Who's there?", voice="aiden")
    _call(engine, text="Who's there?", voice="aiden", instruction="whispering, trembling with fear")
    assert env.seeds[0] == env.seeds[1] == derive_seed("Who's there?", "aiden")


def test_the_seed_follows_text_and_voice(engine, env) -> None:
    _call(engine, text="Who's there?", voice="aiden")
    _call(engine, text="Who is there?", voice="aiden")
    _call(engine, text="Who's there?", voice="ryan")
    assert len(set(env.seeds)) == 3


def test_the_default_voice_and_its_name_share_a_seed(engine, env) -> None:
    """Seeded on the *resolved* speaker, so leaving voice empty is the same take
    as naming the default."""
    _call(engine, text="Hello.")
    _call(engine, text="Hello.", voice="Aiden")
    assert env.seeds[0] == env.seeds[1]


def test_a_callers_seed_wins(engine, env) -> None:
    _call(engine, text="Hello.", seed=1234, instruction="Angry.")
    assert env.seeds == [1234]


def test_the_seed_formula_is_pinned() -> None:
    """Changing the derivation changes every take of every book; make it a
    deliberate, visible edit. 31-bit so it is a valid seed everywhere."""
    assert derive_seed("Hello.", "aiden") == 644269024
    assert 0 <= derive_seed("x" * 5000, "vivian") < 2**31


# ---- through the real engine app ------------------------------------------------

def _wav_info(body: bytes) -> tuple[int, int]:
    with wave.open(io.BytesIO(body)) as w:
        return w.getframerate(), w.getnframes()


def test_lazy_load_uses_the_configured_checkpoint_bf16_and_sdpa(env) -> None:
    client, _ = _client()
    assert client.get("/health").json()["loaded"] is False  # 0 VRAM until first synth
    resp = client.post("/synthesize", json={"text": "Hello there.", "instruction": "Warmly."})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "audio/wav"
    assert _wav_info(resp.content) == (_SR, _SR // 10)
    repo, kwargs = env.loader.loads[0]
    assert repo == str(env.ckpt)
    assert kwargs["dtype"] == "bf16"
    assert kwargs["attn_implementation"] == "sdpa"
    assert kwargs["device_map"] == "cpu"  # the fake reports no CUDA
    assert env.loader.last.calls[0]["instruct"] == "Warmly."


def test_an_unknown_voice_is_a_400(env) -> None:
    client, _ = _client()
    resp = client.post("/synthesize", json={"text": "Hello.", "voice": "morgan_freeman"})
    assert resp.status_code == 400
    assert "aiden" in resp.text  # names the real voices


def test_an_unsupported_language_is_a_400(env) -> None:
    client, _ = _client()
    resp = client.post("/synthesize", json={"text": "Hola.", "language": "nl"})
    assert resp.status_code == 400
    assert "nl" in resp.text


def test_a_non_custom_voice_checkpoint_is_refused_at_load(env) -> None:
    """VoiceDesign/Base are not wired; a 503 naming the problem beats a
    half-working engine that ignores the voice you picked."""
    env.loader.make = lambda: FakeQwen(model_type="voice_design")
    client, _ = _client()
    resp = client.post("/synthesize", json={"text": "Hello."})
    assert resp.status_code == 503
    assert "voice_design" in resp.text and "CustomVoice" in resp.text
    assert "voice_design" in client.get("/health").json()["error"]


def test_unload_frees_the_model_and_the_next_synth_reloads(env) -> None:
    client, eng = _client()
    assert client.post("/synthesize", json={"text": "One."}).status_code == 200
    assert eng._model is not None and client.get("/health").json()["loaded"] is True

    out = client.post("/unload").json()
    assert out == {"id": "qwen3tts", "loaded": False}
    assert eng._model is None  # the reference that pins the VRAM is gone
    assert client.get("/health").json()["loaded"] is False

    assert client.post("/synthesize", json={"text": "Two."}).status_code == 200
    assert len(env.loader.loads) == 2  # reloaded lazily


def test_unload_after_frees_the_model_in_the_same_request(env) -> None:
    client, eng = _client()
    resp = client.post("/synthesize", json={"text": "One.", "unload_after": True})
    assert resp.status_code == 200 and resp.headers["X-Unloaded"] == "1"
    assert eng._model is None


# ---- wiring: the rest of the repo knows this engine ------------------------------

def test_the_gateway_registers_it() -> None:
    from gateway.app.config import DEFAULT_ENGINES

    assert DEFAULT_ENGINES["qwen3tts"] == "http://qwen3tts:8000"


def test_the_studio_static_catalog_matches_the_adapter(monkeypatch) -> None:
    """The studio's copy (pipeline/studio/engines.py) is what Settings/casting
    shows when the container is down; it must say what the adapter says."""
    from pipeline.studio.engines import STATIC_ENGINES

    monkeypatch.setattr(qtts, "_cached_config_path", lambda model: None)
    info = Qwen3TTSEngine().info()
    facts = STATIC_ENGINES["qwen3tts"]
    assert facts["instruction"] is True
    assert facts["audio_tags"] == []
    assert facts["voices"] == [{"id": v.id, "label": v.label} for v in info.voices]
    assert facts["license"] == info.license


def test_the_pipeline_fallback_spec_matches_the_adapter(monkeypatch) -> None:
    from pipeline.direct import DEFAULT_SPECS

    monkeypatch.setattr(qtts, "_cached_config_path", lambda model: None)
    info = Qwen3TTSEngine().info()
    spec = DEFAULT_SPECS["qwen3tts"]
    assert spec["instruction"] is True and spec["audio_tags"] == []
    assert spec["voices"] == [v.id for v in info.voices]
    assert spec["emotion_labels"] == [] and not spec["emotion_tags"] and not spec["emotion_scalar"]
