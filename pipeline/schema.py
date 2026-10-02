"""The Book Script — the pivot artifact of the whole pipeline.

Design rule: keep **intent** (what a scene feels like, engine-agnostic) separate
from **render** (the concrete engine params). That decoupling is what lets us
re-render with a better/expressive engine later without re-running analysis, and
lets Kokoro (which has no emotion knob) map intent → voice/speed/pauses.

Utterances are stored as a **flat, ordered list** (with indices back into
chapters/scenes) because rendering and the player timeline both iterate linearly;
the chapter/scene tree is metadata for navigation.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .profiles import ProductionProfile

SCHEMA_VERSION = "0.1.0"

WIRE_SCHEMA = ConfigDict(json_schema_serialization_defaults_required=True)
"""For the models below that the studio API sends as they are (``Line.intent``,
``Line.render``, ``Line.audio``, ``Line.attribution``, ``CastEntry``). It only
changes the JSON *schema* of a response: a field with a default is always
serialized, so the studio's OpenAPI marks it required instead of optional
(docs/STUDIO-API.md, Amendment v1.4). Validation and the serialized JSON are
untouched, so stored scripts load exactly as before."""

DirectionSource = Literal["none", "director", "manual"]
"""Provenance of a line's direction. Closed: the only writers are the director's
``apply_pass`` ("director", or "manual" for an edited scene) and the studio's line
PATCH ("manual", or "none" when a person clears both fields)."""


class UtteranceKind(str, Enum):
    narration = "narration"
    dialogue = "dialogue"
    internal = "internal"  # unspoken thought
    letter = "letter"
    heading = "heading"
    verse = "verse"


class Intent(BaseModel):
    """Engine-agnostic reading intent. An analyzer fills this in; Kokoro can't
    consume it directly, so the renderer maps it onto voice/speed/pauses."""

    emotion: str = "neutral"
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    pace: float = Field(default=1.0, gt=0.1, le=3.0)  # relative reading speed

    model_config = WIRE_SCHEMA


class Attribution(BaseModel):
    """Where ``Utterance.speaker`` came from, and how much to trust it.

    A wrong speaker is invisible — it sounds like a perfectly good take in the
    wrong voice — while an ``Unknown`` is a work item somebody can see. So the
    attributor records *why* it decided, and abstains (speaker ``"Unknown"``)
    when the basis is weak: see ``attribute.ABSTAIN_BELOW``.

    ``basis``:
      - ``tag``          an explicit ``"…" said X`` / ``X said, "…"`` / ``X: "…"``
      - ``pronoun``      ``"…" she said`` resolved against the recent antecedent
      - ``continuation`` the same speaker continuing inside one paragraph
      - ``alternation``  back-and-forth across paragraphs (a guess, below the bar)
      - ``manual``       a person reassigned the line (studio ``PATCH /lines/{id}``)
      - ``none``         narration, or nothing to go on at all
    """

    basis: Literal["tag", "pronoun", "continuation", "alternation", "manual", "none"] = "none"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    model_config = WIRE_SCHEMA


class Render(BaseModel):
    """Concrete synthesis parameters — this is what becomes a SynthRequest."""

    engine: str = "kokoro"
    voice_id: str | None = None
    speed: float = Field(default=1.0, gt=0.1, le=4.0)
    lead_pause_ms: int = Field(default=0, ge=0)
    trail_pause_ms: int = Field(default=0, ge=0)
    instruction: str | None = None
    """Free-text performance note for an engine that accepts one (Breeze TTS 2).
    Ignored by engines without ``Capabilities.instruction`` — Kokoro included, so
    setting it on a Kokoro render changes nothing but the cache key."""
    spoken_text: str | None = None
    """What to send the engine instead of ``Utterance.text``, for engines that
    take inline vocal events: the engine speaks "(sigh) It was over." while the
    read-along player still shows "It was over." Keeping them apart is the whole
    point — putting tags in ``text`` would print them in the transcript."""
    direction_source: DirectionSource = "none"
    """Provenance of ``instruction``/``spoken_text``: "none" | "director" |
    "manual". Not part of the cache key (it changes nothing audible); it exists so
    a later director pass never overwrites a note a person typed."""
    voice_design: str | None = None
    """Per-character voice description for engines with no named voices (Breeze
    designs a voice from prose). Stamped from ``CastEntry.voice_design`` so the
    same character sounds the same across chapters. Part of the cache key when set."""

    model_config = WIRE_SCHEMA


class AudioSpan(BaseModel):
    """Filled after rendering. Powers the player timeline + incremental re-render."""

    start_ms: int
    end_ms: int
    sample_rate: int
    cache_key: str  # sha256(spoken text + voice + speed + engine + schema + direction)

    model_config = WIRE_SCHEMA


class Utterance(BaseModel):
    id: str
    order: int
    chapter_index: int
    scene_index: int = 0
    speaker: str = "Narrator"
    kind: UtteranceKind = UtteranceKind.narration
    text: str
    attribution: Attribution = Field(default_factory=Attribution)
    """How ``speaker`` was decided. Narration keeps the default
    (``basis="none"``, ``confidence=1.0``) — the narrator is never a guess."""
    intent: Intent = Field(default_factory=Intent)
    render: Render = Field(default_factory=Render)
    audio: AudioSpan | None = None


class Scene(BaseModel):
    index: int
    mode: str = "prose"  # prose | letter | dream | flashback
    pov: str | None = None
    time_frame: str | None = None


class Chapter(BaseModel):
    index: int
    title: str = ""
    kind: Literal["chapter", "front_matter", "back_matter"] = "chapter"
    """Narrative role. A copyright page read aloud in the narrator's voice as
    "Chapter 1" is a defect the schema had no way to express before, so ingest
    now labels it (see ``ingest.mark_matter``)."""
    include_in_audio: bool = True
    """False = skipped by export (and shown as "skipped" in the studio's rail).
    Front matter defaults to False; back matter stays in the audio."""
    scenes: list[Scene] = Field(default_factory=list)
    # Filled by the renderer: the per-chapter audio file + its length. Utterance
    # AudioSpan start/end are relative to this file.
    audio_file: str | None = None
    duration_ms: int = 0


class CastEntry(BaseModel):
    character: str
    voice_id: str | None = None
    engine: str = "kokoro"
    notes: str = ""
    voice_design: str | None = None
    """Prose voice description ("a warm, low adult female voice") for engines that
    design a voice from text instead of picking a named one. None for engines with
    a voice palette (Kokoro), where ``voice_id`` is the whole cast decision."""

    model_config = WIRE_SCHEMA


class BookMeta(BaseModel):
    title: str = ""
    author: str = ""
    language: str = "en"
    source_path: str = ""
    book_type: str = "unknown"  # from classification; drives the production profile


class BookScript(BaseModel):
    schema_version: str = SCHEMA_VERSION
    meta: BookMeta = Field(default_factory=BookMeta)
    profile: ProductionProfile | None = None  # the genre-driven settings used
    cast: list[CastEntry] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    utterances: list[Utterance] = Field(default_factory=list)

    def stats(self) -> dict:
        """A quick human-readable summary (used by the CLI ``--stats`` flag)."""
        words = sum(len(u.text.split()) for u in self.utterances)
        return {
            "schema_version": self.schema_version,
            "title": self.meta.title or "(untitled)",
            "book_type": self.meta.book_type,
            "chapters": len(self.chapters),
            "scenes": sum(len(c.scenes) for c in self.chapters),
            "utterances": len(self.utterances),
            "words": words,
            "est_minutes": round(words / 155, 1),  # ~155 wpm narration
            "cast": [f"{c.character} -> {c.voice_id} ({c.engine})" for c in self.cast],
        }
