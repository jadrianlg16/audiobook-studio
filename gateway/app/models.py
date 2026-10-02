"""Gateway-specific schemas (engine status + bake-off). Engine-contract models
live in ``shared.schemas`` and are reused here."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from shared.schemas import EngineInfo


class EngineStatus(BaseModel):
    id: str
    url: str
    available: bool
    loaded: bool = False  # lazy engines are available before weights are resident
    info: EngineInfo | None = None
    error: str | None = None


class BakeoffItem(BaseModel):
    """One engine's entry in a bake-off, with its per-engine parameters.
    The shared ``text`` lives on ``BakeoffRequest``."""

    engine_id: str
    text: str | None = None
    """Overrides ``BakeoffRequest.text`` for this engine only. The director uses
    it to send a tag-annotated passage ("(sigh) She put the letter down.") to an
    engine that understands the tags, while every other engine still gets the
    plain text — otherwise the tags would be read aloud as literal words."""
    voice: str | None = None
    reference_audio_b64: str | None = None
    reference_text: str | None = None
    emotion: float | None = Field(default=None, ge=0.0, le=1.0)
    emotion_label: str | None = None
    emotion_reference_b64: str | None = None
    instruction: str | None = None
    speed: float = 1.0
    seed: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class BakeoffRequest(BaseModel):
    text: str = Field(..., min_length=1)
    items: list[BakeoffItem] = Field(..., min_length=1)


class BakeoffResult(BaseModel):
    engine_id: str
    ok: bool
    audio_b64: str | None = None
    meta: dict[str, Any] | None = None
    error: str | None = None


class BakeoffResponse(BaseModel):
    results: list[BakeoffResult]


# ---- AI director ------------------------------------------------------------

class DirectorEngineSpec(BaseModel):
    """What an engine can accept — sent so the LLM picks values from its real
    vocabulary instead of a generic guess."""

    id: str
    name: str = ""
    emotion_labels: list[str] = Field(default_factory=list)
    voices: list[str] = Field(default_factory=list)
    audio_tags: list[str] = Field(default_factory=list)
    emotion_scalar: bool = False
    emotion_tags: bool = False
    instruction: bool = False


class DirectorRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    provider: Literal["ollama", "gemini"] = "ollama"
    model: str = Field(..., min_length=1)
    api_key: str | None = None  # Gemini; never stored, used per-request
    ollama_url: str | None = None  # override the gateway default
    engines: list[DirectorEngineSpec] = Field(default_factory=list)


class DirectorSegment(BaseModel):
    speaker: str = "Narrator"
    text: str = ""
    emotion: str = "neutral"


class DirectorEngineDirection(BaseModel):
    """Per-engine choice, validated to that engine's allowed values."""

    engine_id: str
    emotion_label: str | None = None
    voice: str | None = None
    scalar: float = Field(default=0.5, ge=0.0, le=1.0)
    instruction: str | None = None
    """Prose performance note. Only ever set for engines that declared
    ``instruction`` support — otherwise it is dropped in validation."""
    text: str | None = None
    """The passage rewritten with inline audio tags, for engines that declared an
    ``audio_tags`` vocabulary. ``None`` means "synthesize the original text"."""
    rejected_text: str | None = None
    """The model's annotated passage when it *failed* validation — a paraphrase,
    an invented tag, a truncation. Never synthesized; it exists so a review
    screen can show the diff instead of just saying "rejected". Set only when
    ``text_status == "rejected"``."""
    text_status: Literal["kept", "rejected", "none"] = "none"
    """Outcome of the anti-paraphrase check. ``none`` covers both "this engine has
    no tag vocabulary" and "the model added no tags" — neither is a failure, and
    conflating them with ``rejected`` would make the book-scale counts lie."""
    text_reason: str | None = None
    """Why, in one human sentence. At book scale "37 rejected" is unactionable without knowing whether the model
    is paraphrasing, inventing tags, or truncating. Additive and optional."""


class DirectorResult(BaseModel):
    summary: str = ""
    emotion: str = "neutral"  # dominant emotion label (generic / display)
    scalar: float = Field(default=0.5, ge=0.0, le=1.0)  # overall expressiveness
    voice_hint: str = ""
    scene_note: str = ""
    """Context for the **human**: who is in the scene, what is going on under it.
    Never sent to an engine. It used to be conflated with the per-engine
    ``instruction``, so every line of a scene was stamped with "an exchange
    between Elara and Marcus…" — noise to an engine voicing four words of Elara,
    and an invitation to perform both parts."""
    pace: float | None = Field(default=None, ge=0.8, le=1.2)
    """Relative reading speed the director asked for, 0.8-1.2, or None.
    Performable on **every** engine (it is a speed multiplier, not a note), which
    is what makes a director pass audible on Kokoro."""
    segments: list[DirectorSegment] = Field(default_factory=list)
    per_engine: list[DirectorEngineDirection] = Field(default_factory=list)
    prompt_version: str = ""
    """Short content hash of the prompt that produced this result. A book-scale
    pass puts it in the per-scene cache key, so editing ``director.md`` costs a
    re-run and *not* editing it costs nothing."""


class DirectorModel(BaseModel):
    name: str
    size_bytes: int = 0
    modified_at: str = ""


class DirectorModels(BaseModel):
    """What Ollama actually has pulled. A model the box doesn't have should be a
    visible choice in the UI, not a 404 halfway through a book."""

    provider: Literal["ollama"] = "ollama"
    models: list[DirectorModel] = Field(default_factory=list)
    default: str = ""
    error: str | None = None
    """Set (with ``models: []``) when Ollama is unreachable. Deliberately a 200:
    "I could not reach Ollama" is information the UI renders, not a failure of
    the gateway."""
