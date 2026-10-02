"""The engine catalog the UI is allowed to trust — gateway first, static second.

``Settings.engines`` is the contract's source of truth for "what can I route a
line to, what does it understand, which voices does it have" (Amendments v1.1).
The gateway answers that at ``/api/engines``, but it answers it *badly* in the
two situations that matter most: when the gateway is down the list is empty, and
when an engine's container is not running the gateway returns ``info: null``, so
even a reachable gateway can report a known engine with no voices and no tag
vocabulary. A UI built on that list would offer an empty engine dropdown on a
box with no GPU — and, worse, the studio's own validation would then reject the
engine ids the pipeline itself assigns by default.

So the catalog is a **merge**: the static facts below (copied from each engine
adapter's own ``info()``) fill in every field the gateway did not answer, and
``available`` always comes from the gateway (static knowledge can never claim a
container is up). Where both have a value, the gateway wins — a tag list edited
in an engine's environment (``BREEZE_AUDIO_TAGS``) must not be overridden by a
stale copy here.

The duplication is deliberate and bounded: ids, names, licences, voice lists and
tag vocabularies for the engines this repo ships. ``pipeline/tests/test_studio.py``
pins the ones that carry rules (breeze's tags, kokoro's voices) so a drift shows
up as a failing test rather than as a mysterious 422.
"""

from __future__ import annotations

from typing import Iterable

from ..cast import DEFAULT_ENGINE_VOICES, non_commercial_engines
from .models import EngineOption

# Voice labels as the engines themselves publish them (engines/<id>/tts.py).
_KOKORO_VOICES: list[tuple[str, str]] = [
    ("af_heart", "Heart · US Female"),
    ("af_bella", "Bella · US Female"),
    ("af_nicole", "Nicole · US Female"),
    ("am_michael", "Michael · US Male"),
    ("am_fenrir", "Fenrir · US Male"),
    ("bf_emma", "Emma · UK Female"),
    ("bm_george", "George · UK Male"),
]

# engines/breeze/tts.py::_DEFAULT_AUDIO_TAGS — the four events the open weights
# attest (docs/BREEZE-AUDIT-2026-09-05.md). The hosted product lists more, for a
# different model in a different syntax; BREEZE_AUDIO_TAGS widens this per
# deployment, which is why the gateway's list wins when it answers.
_BREEZE_TAGS = ["(laugh)", "(cough)", "(sigh)", "(clears throat)"]

_ORPHEUS_VOICES = ["tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe"]

# engines/qwen3tts/tts.py — the nine CustomVoice speakers as its /info labels them,
# default (an English native) first. The labels carry each speaker's native
# language because upstream recommends casting speakers in their own language.
# engines/qwen3tts/tests pins this copy against the adapter.
_QWEN3TTS_VOICES: list[tuple[str, str]] = [
    ("aiden", "Aiden · US male, clear midrange · English native (default)"),
    ("ryan", "Ryan · male, strong rhythmic drive · English native"),
    ("vivian", "Vivian · young female, bright, slightly edgy · Chinese native"),
    ("serena", "Serena · young female, warm, gentle · Chinese native"),
    ("uncle_fu", "Uncle Fu · seasoned male, low, mellow · Chinese native"),
    ("dylan", "Dylan · young male, clear, natural · Beijing-dialect Chinese native"),
    ("eric", "Eric · male, lively, slightly husky · Sichuan-dialect Chinese native"),
    ("ono_anna", "Ono Anna · female, playful, light · Japanese native"),
    ("sohee", "Sohee · female, warm, rich emotion · Korean native"),
]

STATIC_ENGINES: dict[str, dict] = {
    "mock": {
        "name": "Mock Tone",
        "instruction": False,
        "audio_tags": [],
        "voices": [
            {"id": "low", "label": "Low (220 Hz)"},
            {"id": "mid", "label": "Mid (440 Hz)"},
            {"id": "high", "label": "High (660 Hz)"},
        ],
        "license": "n/a (no model weights)",
    },
    "kokoro": {
        "name": "Kokoro-82M",
        "instruction": False,
        "audio_tags": [],
        "voices": [{"id": v, "label": label} for v, label in _KOKORO_VOICES],
        "license": "Apache-2.0",
    },
    "breeze": {
        "name": "Breeze TTS 2",
        "instruction": True,
        "audio_tags": list(_BREEZE_TAGS),
        "voices": [],  # designs a voice from prose instead of naming one
        "license": "BreezeBlue Research & Non-Commercial (weights)",
    },
    "qwen3tts": {
        "name": "Qwen3-TTS",
        "instruction": True,  # the note goes to the model's `instruct`
        "audio_tags": [],  # the open weights attest no inline event vocabulary
        "voices": [{"id": v, "label": label} for v, label in _QWEN3TTS_VOICES],
        "license": "Apache-2.0 (weights and code)",
    },
    "indextts2": {
        "name": "IndexTTS 2",
        "instruction": True,
        "audio_tags": [],
        "voices": [],
        "license": "bilibili Model Use License (see upstream LICENSE)",
    },
    "chatterbox": {
        "name": "Chatterbox",
        "instruction": False,
        "audio_tags": [],
        "voices": [],
        "license": "MIT",
    },
    "styletts2": {
        "name": "StyleTTS 2",
        "instruction": False,
        "audio_tags": [],
        "voices": [],
        "license": "MIT code; pretrained-model terms: disclose synthetic speech",
    },
    "orpheus": {
        "name": "Orpheus 3B",
        "instruction": False,
        "audio_tags": [],
        "voices": [{"id": v, "label": v.capitalize()} for v in _ORPHEUS_VOICES],
        "license": "Apache-2.0 (model card); fine-tuned from Llama 3.2",
    },
}

# Kokoro's cast-side voice palette and its published voice list must agree, or
# `assign_engines` hands out a voice_id that `PATCH /lines/{id}` then rejects.
_palette = set(DEFAULT_ENGINE_VOICES.get("kokoro") or ())
_published = {v["id"] for v in STATIC_ENGINES["kokoro"]["voices"]}
for _extra in sorted(_palette - _published):
    STATIC_ENGINES["kokoro"]["voices"].append({"id": _extra, "label": _extra})


def static_options() -> list[EngineOption]:
    """The catalog with no gateway at all: known, unavailable, fully described."""
    flagged = non_commercial_engines()
    return [
        EngineOption(
            id=engine_id,
            name=str(facts.get("name") or engine_id),
            available=False,
            instruction=bool(facts.get("instruction")),
            audio_tags=list(facts.get("audio_tags") or []),
            voices=[dict(v) for v in (facts.get("voices") or [])],
            license=str(facts.get("license") or ""),
            non_commercial=engine_id in flagged,
        )
        for engine_id, facts in STATIC_ENGINES.items()
    ]


def merge_options(live: Iterable[EngineOption]) -> list[EngineOption]:
    """Gateway answers first, static facts filling the gaps, static-only last.

    ``available`` is the gateway's word alone: an engine the gateway never
    mentioned is reported as a known-but-unavailable engine rather than hidden,
    because hiding it is what leaves the UI with an empty dropdown while the
    pipeline is still assigning lines to it.
    """
    out: list[EngineOption] = []
    seen: set[str] = set()
    for option in live:
        if not option.id or option.id in seen:
            continue
        seen.add(option.id)
        facts = STATIC_ENGINES.get(option.id) or {}
        out.append(
            EngineOption(
                id=option.id,
                name=option.name or str(facts.get("name") or option.id),
                available=option.available,
                instruction=option.instruction or bool(facts.get("instruction")),
                audio_tags=option.audio_tags or list(facts.get("audio_tags") or []),
                voices=option.voices or [dict(v) for v in (facts.get("voices") or [])],
                license=option.license or str(facts.get("license") or ""),
                non_commercial=option.non_commercial,
            )
        )
    for option in static_options():
        if option.id not in seen:
            out.append(option)
    out.sort(key=lambda e: (not e.available, e.id))
    return out


def by_id(options: Iterable[EngineOption]) -> dict[str, EngineOption]:
    return {o.id: o for o in options}


def tags_by_engine(options: Iterable[EngineOption]) -> dict[str, list[str]]:
    """``{engine_id: audio_tags}`` — the map ``render.py`` gates ``spoken_text``
    on (contract rule 2: tags only ever reach an engine that advertises them)."""
    return {o.id: list(o.audio_tags) for o in options}
