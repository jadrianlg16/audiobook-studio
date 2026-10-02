"""Qwen3-TTS adapter (Alibaba Qwen) — named voices you can *direct* in prose.

The CustomVoice checkpoints ship nine named speakers and take an optional
free-text ``instruct`` ("whispering, trembling with fear") that steers timbre,
emotion and prosody for that line. That makes this the second engine here, after
Breeze, that can consume the AI director's written note — and the first whose
weights are Apache-2.0, so its output can go into a book you publish.

Unlike Breeze this adapter imports the model in-process: ``qwen-tts`` ships a
documented Python API (``Qwen3TTSModel``), so there is no server to spawn.

What is wired, and what is deliberately not:

- **CustomVoice only** (the default, ``Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice``).
  ``QWEN_TTS_MODEL`` may point at another CustomVoice checkpoint (the 0.6B one,
  or a local directory). A VoiceDesign or Base checkpoint is refused at load
  with a clear error rather than half-supported.
- **No cloning.** Cloning needs the separate Base checkpoint (another ~4.5 GB),
  a second model resident or a model swap, and the reference transcript. Not
  cheap on an 8 GB card, never run here — so ``Capabilities.cloning`` is False.
- **No audio tags.** Nothing in the open weights, the model card or the
  ``qwen-tts`` package attests an inline event vocabulary (``(sigh)`` and
  friends). The Breeze lesson (docs/BREEZE-AUDIT-2026-09-05.md): advertise only what the weights
  attest. An empty list means the director never inserts one.
- **No speed control.** ``generate_custom_voice`` has no rate parameter;
  ``req.speed`` is ignored, as Breeze ignores it. Ask for pace in the
  instruction ("speak slowly") — the model card lists speaking rate among the
  attributes ``instruct`` controls.
- **0.6B CustomVoice ignores ``instruct``** (upstream drops it:
  ``if tts_model_size in "0b6": instruct = None``), so on that checkpoint
  ``Capabilities.instruction`` is reported False.

Seeding (why and how): sampling is on by default (``do_sample=True``,
temperature 0.9), so two takes of the same line differ — which would swamp an
A/B test of direction. Every request is therefore seeded: ``req.seed`` when the
caller sets one, otherwise a seed derived from ``sha256(voice + text)``. The
instruction is deliberately *not* part of the hash, so the same line with and
without a note starts from the same random state and the note is the only
variable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass

import numpy as np

from shared.engine_base import TTSEngine
from shared.schemas import Capabilities, EngineInfo, SynthRequest, VoiceInfo

log = logging.getLogger("uvicorn.error")  # shows up in `docker compose logs`

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_VOICE = "aiden"  # "Sunny American male voice with a clear midrange" — an English native

# The nine CustomVoice speakers, in the order /info lists them (English natives
# first, since that is what this repo narrates). id = the checkpoint's own key
# (config.json talker_config.spk_id); description and native language are the
# model card's. The model card: "We recommend using each speaker's native
# language for the best quality. Of course, each speaker can speak any language
# supported by the model."
# (id, display name, short description, native language label, ISO code)
_SPEAKER_CARD: list[tuple[str, str, str, str, str]] = [
    ("aiden", "Aiden", "US male, clear midrange", "English", "en"),
    ("ryan", "Ryan", "male, strong rhythmic drive", "English", "en"),
    ("vivian", "Vivian", "young female, bright, slightly edgy", "Chinese", "zh"),
    ("serena", "Serena", "young female, warm, gentle", "Chinese", "zh"),
    ("uncle_fu", "Uncle Fu", "seasoned male, low, mellow", "Chinese", "zh"),
    ("dylan", "Dylan", "young male, clear, natural", "Beijing-dialect Chinese", "zh"),
    ("eric", "Eric", "male, lively, slightly husky", "Sichuan-dialect Chinese", "zh"),
    ("ono_anna", "Ono Anna", "female, playful, light", "Japanese", "ja"),
    ("sohee", "Sohee", "female, warm, rich emotion", "Korean", "ko"),
]
_CARD_BY_ID = {row[0]: row for row in _SPEAKER_CARD}

# The model names languages in English words; the contract uses ISO codes.
_ISO_TO_MODEL: dict[str, str] = {
    "en": "english",
    "zh": "chinese",
    "ja": "japanese",
    "ko": "korean",
    "de": "german",
    "fr": "french",
    "ru": "russian",
    "pt": "portuguese",
    "es": "spanish",
    "it": "italian",
}
_MODEL_TO_ISO = {v: k for k, v in _ISO_TO_MODEL.items()}

# Generation knobs a caller may override through ``SynthRequest.extra`` (all are
# ``generate_custom_voice`` kwargs; defaults come from the checkpoint's own
# generation_config.json). Anything else in ``extra`` is ignored.
_GEN_KEYS = (
    "do_sample",
    "temperature",
    "top_k",
    "top_p",
    "repetition_penalty",
    "max_new_tokens",
    "subtalker_dosample",
    "subtalker_temperature",
    "subtalker_top_k",
    "subtalker_top_p",
)


@dataclass(frozen=True)
class ModelFacts:
    """What the configured checkpoint can do — from its config.json when we can
    read it, else the default checkpoint's published facts."""

    model_type: str = "custom_voice"
    model_size: str = "1b7"
    speakers: tuple[str, ...] = tuple(row[0] for row in _SPEAKER_CARD)
    languages: tuple[str, ...] = tuple(_ISO_TO_MODEL.values())  # model names, no "auto"
    source: str = "fallback"

    @property
    def instruction(self) -> bool:
        # Upstream drops instruct on the 0.6B CustomVoice ("0b6").
        return self.model_type == "custom_voice" and self.model_size != "0b6"


def derive_seed(text: str, voice: str) -> int:
    """Seed for a request that did not bring its own: a hash of voice + text.

    NOT the instruction — the same line with and without a note must start from
    the same random state, or an A/B of direction measures sampling noise.
    31 bits so it is a valid seed everywhere (torch, numpy, JSON)."""
    digest = hashlib.sha256(f"{voice}\x00{text}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _seed_torch(seed: int) -> None:
    """Seed every RNG the sampler touches. ``torch.manual_seed`` also seeds all
    CUDA devices. Module-level so the offline tests can replace it."""
    import random

    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def _model_id() -> str:
    return os.environ.get("QWEN_TTS_MODEL", "").strip() or DEFAULT_MODEL


def _cached_config_path(model: str) -> str | None:
    """config.json for ``model`` if it is already on disk (a local directory, or
    the HF cache) — never downloads, so ``info()`` stays cheap."""
    if os.path.isdir(model):
        path = os.path.join(model, "config.json")
        return path if os.path.isfile(path) else None
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    try:
        path = try_to_load_from_cache(model, "config.json")
    except Exception:  # noqa: BLE001 - an odd repo id is "not cached", not a crash
        return None
    return path if isinstance(path, str) else None


def _facts_from_config(cfg: dict, source: str) -> ModelFacts:
    talker = cfg.get("talker_config") or {}
    speakers = tuple(str(s).lower() for s in (talker.get("spk_id") or {}))
    # Mirrors Qwen3TTSForConditionalGeneration.__init__: dialect entries are
    # reached through a dialect speaker, not requested as a language.
    languages = tuple(
        str(k).lower() for k in (talker.get("codec_language_id") or {}) if "dialect" not in str(k)
    )
    return ModelFacts(
        model_type=str(cfg.get("tts_model_type") or "custom_voice"),
        model_size=str(cfg.get("tts_model_size") or ""),
        speakers=speakers,
        languages=languages,
        source=source,
    )


class Qwen3TTSEngine(TTSEngine):
    SR = 24_000  # the 12 Hz speech tokenizer decodes to 24 kHz mono

    def __init__(self) -> None:
        super().__init__()
        self._model = None  # qwen_tts.Qwen3TTSModel once loaded
        self._facts_cache: ModelFacts | None = None
        self.load_seconds: float | None = None

    # ---- what the checkpoint can do ------------------------------------------

    def facts(self) -> ModelFacts:
        """The loaded model's own answer, else its config.json, else the card."""
        if self._model is not None:
            inner = getattr(self._model, "model", None)
            return ModelFacts(
                model_type=str(getattr(inner, "tts_model_type", "custom_voice")),
                model_size=str(getattr(inner, "tts_model_size", "")),
                speakers=tuple(self._model.get_supported_speakers() or ()),
                languages=tuple(
                    lang for lang in (self._model.get_supported_languages() or ()) if lang != "auto"
                ),
                source="loaded model",
            )
        if self._facts_cache is not None:
            return self._facts_cache
        path = _cached_config_path(_model_id())
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    self._facts_cache = _facts_from_config(json.load(fh), f"config.json ({path})")
                return self._facts_cache
            except (OSError, ValueError):
                pass
        # Not downloaded yet: report the default checkpoint's published facts
        # (the model card + its config.json, checked 2026-09-26). Not cached, so
        # the real config wins as soon as the weights land.
        return ModelFacts()

    def default_voice(self, speakers: tuple[str, ...] | None = None) -> str:
        speakers = speakers if speakers is not None else self.facts().speakers
        wanted = os.environ.get("QWEN_TTS_DEFAULT_VOICE", "").strip().lower() or DEFAULT_VOICE
        if wanted in speakers or not speakers:
            return wanted
        return _ordered(speakers)[0]

    # ---- metadata ------------------------------------------------------------

    def info(self) -> EngineInfo:
        facts = self.facts()
        default = self.default_voice(facts.speakers)
        ordered = [default] + [s for s in _ordered(facts.speakers) if s != default]
        voices = [_voice_info(s, default=(s == default)) for s in ordered if s]
        languages = [_MODEL_TO_ISO.get(lang, lang) for lang in facts.languages]
        languages.sort(key=lambda code: (code != "en", code))
        return EngineInfo(
            id="qwen3tts",
            name="Qwen3-TTS",
            description=(
                "Nine named voices, each directable with a written performance note "
                "('whispering, trembling with fear'). Ten languages. Apache-2.0 weights. "
                "Seeded per line, so a take with and without a note differ only by the note."
            ),
            license="Apache-2.0 (weights and code)",
            homepage="https://huggingface.co/" + DEFAULT_MODEL,
            sample_rate=self.SR,
            capabilities=Capabilities(
                instruction=facts.instruction,
                multi_speaker=bool(voices),
                cloning=False,  # Base checkpoint only; not wired (see module docstring)
                requires_gpu=True,
                languages=languages or ["en"],
            ),
            voices=voices,
            # Nothing in the open weights attests an inline event vocabulary.
            audio_tags=[],
        )

    # ---- lifecycle -----------------------------------------------------------

    def _load(self) -> None:
        import torch
        from qwen_tts import Qwen3TTSModel

        model_id = _model_id()
        device = os.environ.get("QWEN_TTS_DEVICE", "").strip() or (
            "cuda:0" if torch.cuda.is_available() else "cpu"
        )
        dtype_name = os.environ.get("QWEN_TTS_DTYPE", "").strip() or "bfloat16"
        dtype = getattr(torch, dtype_name, None)
        if dtype is None:
            raise RuntimeError(f"QWEN_TTS_DTYPE={dtype_name!r} is not a torch dtype")
        # sdpa: PyTorch's fused attention. flash-attn is not built into the image
        # (upstream recommends it for memory, but it is optional).
        attn = os.environ.get("QWEN_TTS_ATTN", "").strip() or "sdpa"

        started = time.perf_counter()
        model = Qwen3TTSModel.from_pretrained(
            model_id, device_map=device, dtype=dtype, attn_implementation=attn
        )
        model_type = getattr(model.model, "tts_model_type", None)
        if model_type != "custom_voice":
            raise RuntimeError(
                f"{model_id} is a {model_type!r} checkpoint; this adapter drives the "
                f"CustomVoice checkpoints only (named speakers + instruct). See "
                f"engines/qwen3tts/README.md."
            )
        self._model = model
        self.load_seconds = time.perf_counter() - started
        log.info(
            "qwen3tts: loaded %s on %s (%s, attn=%s) in %.1f s",
            model_id, device, dtype_name, attn, self.load_seconds,
        )

    def _unload(self) -> None:
        # The base class then runs gc + torch.cuda.empty_cache().
        self._model = None

    # ---- synthesis -----------------------------------------------------------

    def resolve_voice(self, voice: str | None, speakers: tuple[str, ...]) -> str:
        if not voice or not voice.strip():
            return self.default_voice(speakers)
        wanted = voice.strip().lower()
        if speakers and wanted not in speakers:
            raise ValueError(
                f"Qwen3-TTS has no voice {voice!r}; its voices are: {', '.join(_ordered(speakers))}"
            )
        return wanted

    @staticmethod
    def resolve_language(language: str | None, languages: tuple[str, ...]) -> str:
        """ISO code ("en") or the model's own name ("English"), or "auto"."""
        raw = (language or "").strip().lower()
        if not raw or raw == "auto":
            return "Auto"
        name = _ISO_TO_MODEL.get(raw, raw)
        if languages and name not in languages:
            supported = ", ".join(sorted(_MODEL_TO_ISO.get(n, n) for n in languages))
            raise ValueError(f"Qwen3-TTS does not speak {language!r}; supported: {supported}, auto")
        return name.capitalize()

    def seed_for(self, req: SynthRequest, voice: str) -> int:
        return req.seed if req.seed is not None else derive_seed(req.text, voice)

    def synthesize(self, req: SynthRequest) -> np.ndarray:
        facts = self.facts()
        voice = self.resolve_voice(req.voice, facts.speakers)
        language = self.resolve_language(req.language, facts.languages)
        instruct = (req.instruction or "").strip() or None
        if instruct and not facts.instruction:
            # 0.6B: upstream would drop it anyway; say so in the log, not silently.
            log.info("qwen3tts: %s ignores instruct; dropping the note", _model_id())
            instruct = None
        gen = {k: req.extra[k] for k in _GEN_KEYS if k in req.extra}
        # NOTE: req.speed is ignored — the model has no rate control. Put pace in
        # the instruction. emotion / emotion_label / reference clips are ignored
        # too (not declared in Capabilities).

        seed = self.seed_for(req, voice)
        _seed_torch(seed)
        log.info(
            "qwen3tts: voice=%s lang=%s seed=%d%s instruct=%r chars=%d",
            voice, language, seed, "" if req.seed is None else " (caller)", instruct, len(req.text),
        )
        wavs, sr = self._model.generate_custom_voice(
            text=req.text,
            speaker=voice,
            language=language,
            instruct=instruct,
            non_streaming_mode=True,
            **gen,
        )
        if int(sr) != self.SR:
            raise RuntimeError(f"Qwen3-TTS returned {sr} Hz audio; this adapter declares {self.SR} Hz")
        if not wavs:
            return np.zeros(0, dtype=np.float32)
        return np.asarray(wavs[0], dtype=np.float32).reshape(-1)


def _ordered(speakers) -> list[str]:
    """Card order (English natives first), then anything the card doesn't know."""
    known = [row[0] for row in _SPEAKER_CARD if row[0] in speakers]
    return known + sorted(s for s in speakers if s not in _CARD_BY_ID)


def _voice_info(speaker: str, *, default: bool = False) -> VoiceInfo:
    row = _CARD_BY_ID.get(speaker)
    if row is None:
        return VoiceInfo(id=speaker, label=speaker.replace("_", " ").title(), language="und")
    _, name, desc, native, iso = row
    label = f"{name} · {desc} · {native} native"
    if default:
        label += " (default)"
    return VoiceInfo(id=speaker, label=label, language=iso)
